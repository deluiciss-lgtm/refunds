"""
=============================================================================
 SUPPORT / ORDER-TRACKER BOT  (discord.py)
=============================================================================
 A modern, config-driven support ticket bot for your own store.

 FLOW (each step updates one tidy private message, with progress bars):
   1. /setup posts a panel with an "Open a Support Ticket" button.
   2. User picks STORE REGION/DOMAIN   (dropdown, step 1)
   3. picks ISSUE TYPE                  (dropdown, step 2)
   4. picks PRODUCT                     (dropdown, step 3)
   5. sees a SALE REVEAL  (base price struck out -> % off -> total -> deposit)
   6. fills FORMS: Order info -> Your details -> Shipping -> Billing
   7. CONFIRMS a summary
   8. -> a private ticket channel opens with a UNIQUE ORDER NUMBER,
        the number is DM'd to them, and they can reopen it with /lookup.

 All text/colors/regions/issues/products/prices live in config.yaml.
 No database needed: the order number is stored in the channel name + topic,
 so /lookup keeps working even after the bot restarts or redeploys.
=============================================================================
"""

import os
import random
import string
import asyncio

import yaml
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ---- 1) Token + config ------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit(
        "ERROR: No DISCORD_TOKEN found. Set it as an environment variable "
        "on your host, or create a .env file locally (copy .env.example)."
    )

with open("config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)


# ---- 2) Small helpers -------------------------------------------------------
def cfg(*keys, default=None):
    node = CONFIG
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return default
    return node


def color(name):
    hexstr = cfg("appearance", name, default="#5865F2")
    return discord.Color(int(str(hexstr).lstrip("#"), 16))


def emoji_or_none(value):
    value = (value or "").strip()
    return value or None


def money(amount, symbol):
    if float(amount).is_integer():
        return f"{symbol}{int(amount):,}"
    return f"{symbol}{amount:,.2f}"


def progress_bar(n, total):
    """Returns a little bar like ▰▰▱▱ for step n of total."""
    filled = "▰" * n
    empty = "▱" * (total - n)
    return filled + empty


def order_number():
    """Unique-ish human-friendly id, e.g. ORD-7K2Q9X."""
    chars = string.ascii_uppercase + string.digits
    return "ORD-" + "".join(random.choices(chars, k=6))


def find_region(region_id):
    for r in cfg("regions", default=[]):
        if r["id"] == region_id:
            return r
    return {}


def find_issue(issue_id):
    for i in cfg("issues", default=[]):
        if i["id"] == issue_id:
            return i
    return {}


def find_product(product_id):
    for p in cfg("products", default=[]):
        if p["id"] == product_id:
            return p
    return {}


# ---- 3) The order/ticket state we carry through the wizard ------------------
class Ticket:
    def __init__(self):
        self.region_id = None
        self.issue_id = None
        self.product_id = None
        # form data filled in along the way:
        self.order = {}      # order_number, total, date, delivery, items
        self.personal = {}   # name, email, phone, notes
        self.shipping = {}    # street, city, state, zip, country
        self.billing = {}     # same fields (or copied from shipping)
        self.billing_same = False

    # --- shortcuts into config ---
    @property
    def region(self): return find_region(self.region_id)
    @property
    def issue(self): return find_issue(self.issue_id)
    @property
    def product(self): return find_product(self.product_id)
    @property
    def symbol(self): return self.region.get("symbol", "$")

    @property
    def base_price(self):
        return self.product.get("prices", {}).get(self.region_id, 0) or 0

    @property
    def discount_percent(self):
        # product override > global sale
        return self.product.get("discount_percent",
                                 cfg("sale", "discount_percent", default=0))

    @property
    def deposit_percent(self):
        return cfg("sale", "deposit_percent", default=0)

    @property
    def final_total(self):
        return self.base_price * (100 - self.discount_percent) / 100

    @property
    def deposit(self):
        return self.base_price * self.deposit_percent / 100

    @property
    def savings(self):
        return self.base_price - self.final_total


# ---- 4) Embed builders ------------------------------------------------------
def step_embed(prompt, n, total):
    bar = progress_bar(n, total)
    e = discord.Embed(
        title=cfg("text", "steps", "title", default="New Ticket"),
        description=prompt,
        color=color("primary_color"),
    )
    e.set_footer(text=cfg("text", "steps", "progress", default="{bar} {n}/{total}")
                 .format(bar=bar, n=n, total=total))
    return e


def sale_embed(t: Ticket):
    e = discord.Embed(
        title=cfg("text", "sale", "title", default="Your Deal"),
        color=color("sale_color"),
    )
    base = money(t.base_price, t.symbol)
    final = money(t.final_total, t.symbol)
    deposit = money(t.deposit, t.symbol)
    savings = money(t.savings, t.symbol)
    lines = [
        cfg("text", "sale", "line_was").format(base=base),
        cfg("text", "sale", "line_off").format(percent=t.discount_percent),
        cfg("text", "sale", "line_total").format(final=final),
        cfg("text", "sale", "line_deposit").format(deposit=deposit),
        cfg("text", "sale", "line_savings").format(savings=savings),
    ]
    e.description = (
        f"{t.product.get('emoji','')} **{t.product.get('label','')}** "
        f"· {t.region.get('label','')}\n\n" + "\n".join(lines)
    )
    return e


def address_block(addr):
    if not addr:
        return "—"
    parts = [
        addr.get("street", ""),
        f"{addr.get('city','')}, {addr.get('state','')} {addr.get('zip','')}".strip(", "),
        addr.get("country", ""),
    ]
    return "\n".join(p for p in parts if p.strip())


def summary_embed(t: Ticket, title, col, order_no=None):
    e = discord.Embed(title=title, color=col)
    if cfg("text", "confirmation", "description") and order_no is None:
        e.description = cfg("text", "confirmation", "description")

    e.add_field(name="🏬 Store / Region",
                value=f"{t.region.get('label','?')}\n`{t.region.get('domain','')}`",
                inline=True)
    e.add_field(name="🛠️ Issue",
                value=f"{t.issue.get('emoji','')} {t.issue.get('label','?')}",
                inline=True)
    e.add_field(name="📦 Product",
                value=f"{t.product.get('emoji','')} {t.product.get('label','?')}",
                inline=True)

    # Sale block
    if cfg("sale", "enabled", default=True) and t.base_price:
        e.add_field(
            name="💰 Your Deal",
            value=(f"~~{money(t.base_price, t.symbol)}~~ → "
                   f"**{money(t.final_total, t.symbol)}** ({t.discount_percent}% off)\n"
                   f"💳 Due upfront: **{money(t.deposit, t.symbol)}**"),
            inline=False)

    # Order info
    o = t.order
    e.add_field(
        name="📋 Order Information",
        value=(f"**Order #:** {o.get('order_number','—')}\n"
               f"**Total:** {o.get('order_total','—')}\n"
               f"**Ordered:** {o.get('order_date','—')}\n"
               f"**Delivery:** {o.get('delivery_date','—') or '—'}\n"
               f"**Items:** {o.get('items','—')}"),
        inline=False)

    # Personal
    p = t.personal
    e.add_field(
        name="👤 Your Details",
        value=(f"**Name:** {p.get('name','—')}\n"
               f"**Email:** {p.get('email','—') or '—'}\n"
               f"**Phone:** {p.get('phone','—')}"),
        inline=False)

    e.add_field(name="📦 Shipping", value=address_block(t.shipping), inline=True)
    e.add_field(name="🧾 Billing",
                value="Same as shipping" if t.billing_same else address_block(t.billing),
                inline=True)

    if p.get("notes"):
        e.add_field(name="📝 Other Details", value=p["notes"], inline=False)

    footer = cfg("text", "confirmation", "footer")
    if footer:
        e.set_footer(text=footer)
    return e


# =============================================================================
# 5) THE DROPDOWN STEPS  (region -> issue -> product)
# =============================================================================
class RegionSelect(discord.ui.Select):
    def __init__(self, t):
        self.t = t
        options = [discord.SelectOption(label=r["label"], value=r["id"],
                                        emoji=emoji_or_none(r.get("emoji")))
                   for r in cfg("regions", default=[])]
        super().__init__(placeholder=cfg("text", "steps", "choose_region"),
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        self.t.region_id = self.values[0]
        await interaction.response.edit_message(
            embed=step_embed(cfg("text", "steps", "choose_issue"), 2, 3),
            view=IssueView(self.t))


class IssueSelect(discord.ui.Select):
    def __init__(self, t):
        self.t = t
        options = [discord.SelectOption(label=i["label"], value=i["id"],
                                        emoji=emoji_or_none(i.get("emoji")))
                   for i in cfg("issues", default=[])]
        super().__init__(placeholder=cfg("text", "steps", "choose_issue"),
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        self.t.issue_id = self.values[0]
        await interaction.response.edit_message(
            embed=step_embed(cfg("text", "steps", "choose_product"), 3, 3),
            view=ProductView(self.t))


class ProductSelect(discord.ui.Select):
    def __init__(self, t):
        self.t = t
        options = []
        for p in cfg("products", default=[]):
            price = p.get("prices", {}).get(t.region_id)
            desc = money(price, t.symbol) if price is not None else None
            options.append(discord.SelectOption(
                label=p["label"], value=p["id"], description=desc,
                emoji=emoji_or_none(p.get("emoji"))))
        super().__init__(placeholder=cfg("text", "steps", "choose_product"),
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction):
        self.t.product_id = self.values[0]
        # Reward moment: reveal the deal, then a Continue button.
        if cfg("sale", "enabled", default=True) and self.t.base_price:
            await interaction.response.edit_message(
                embed=sale_embed(self.t), view=SaleContinueView(self.t))
        else:
            await interaction.response.send_modal(OrderModal(self.t))


class RegionView(discord.ui.View):
    def __init__(self, t):
        super().__init__(timeout=900); self.add_item(RegionSelect(t))

class IssueView(discord.ui.View):
    def __init__(self, t):
        super().__init__(timeout=900); self.add_item(IssueSelect(t))

class ProductView(discord.ui.View):
    def __init__(self, t):
        super().__init__(timeout=900); self.add_item(ProductSelect(t))


class SaleContinueView(discord.ui.View):
    """The 'Continue ➜' button shown under the deal reveal."""
    def __init__(self, t):
        super().__init__(timeout=900); self.t = t
        btn = discord.ui.Button(
            label=cfg("text", "sale", "continue_button", "label", default="Continue"),
            emoji=emoji_or_none(cfg("text", "sale", "continue_button", "emoji")),
            style=discord.ButtonStyle.success)
        btn.callback = self.cont
        self.add_item(btn)

    async def cont(self, interaction):
        await interaction.response.send_modal(OrderModal(self.t))


# =============================================================================
# 6) THE FORMS  (chained popups, each <= 5 fields)
# =============================================================================
def ti(label, placeholder="", required=True, style=discord.TextStyle.short,
       max_length=200, default=""):
    return discord.ui.TextInput(label=label[:45], placeholder=placeholder[:100],
                                required=required, style=style,
                                max_length=max_length, default=default)


class OrderModal(discord.ui.Modal):
    def __init__(self, t):
        super().__init__(title=cfg("text", "forms", "order", "title")[:45]); self.t = t
        f = lambda k: cfg("text", "forms", "order", k, default=k)
        self.order_number = ti(f("order_number"), f("order_number_ph"))
        self.order_total = ti(f("order_total"), f("order_total_ph"))
        self.order_date = ti(f("order_date"), f("order_date_ph"))
        self.delivery_date = ti(f("delivery_date"), f("delivery_date_ph"), required=False)
        self.items = ti(f("items"), f("items_ph"),
                        style=discord.TextStyle.paragraph, max_length=500)
        for x in (self.order_number, self.order_total, self.order_date,
                  self.delivery_date, self.items):
            self.add_item(x)

    async def on_submit(self, interaction):
        self.t.order = {
            "order_number": self.order_number.value.strip(),
            "order_total": self.order_total.value.strip(),
            "order_date": self.order_date.value.strip(),
            "delivery_date": self.delivery_date.value.strip(),
            "items": self.items.value.strip(),
        }
        await edit_to_next(interaction, "✅ Order info saved!", PersonalContinueView(self.t))


class PersonalModal(discord.ui.Modal):
    def __init__(self, t):
        super().__init__(title=cfg("text", "forms", "personal", "title")[:45]); self.t = t
        f = lambda k: cfg("text", "forms", "personal", k, default=k)
        self.name = ti(f("name"), f("name_ph"))
        self.email = ti(f("email"), f("email_ph"), required=False)
        self.phone = ti(f("phone"), f("phone_ph"))
        self.notes = ti(f("notes"), f("notes_ph"), required=False,
                        style=discord.TextStyle.paragraph, max_length=500)
        for x in (self.name, self.email, self.phone, self.notes):
            self.add_item(x)

    async def on_submit(self, interaction):
        self.t.personal = {
            "name": self.name.value.strip(),
            "email": self.email.value.strip(),
            "phone": self.phone.value.strip(),
            "notes": self.notes.value.strip(),
        }
        await edit_to_next(interaction, "✅ Your details saved!", ShippingContinueView(self.t))


class AddressModal(discord.ui.Modal):
    """Used for both shipping and billing (kind = 'shipping' or 'billing')."""
    def __init__(self, t, kind):
        super().__init__(title=cfg("text", "forms", kind, "title")[:45])
        self.t = t; self.kind = kind
        f = lambda k: cfg("text", "forms", kind, k, default=k)
        self.street = ti(f("street"))
        self.city = ti(f("city"))
        self.state = ti(f("state"))
        self.zip = ti(f("zip"))
        self.country = ti(f("country"))
        for x in (self.street, self.city, self.state, self.zip, self.country):
            self.add_item(x)

    async def on_submit(self, interaction):
        data = {"street": self.street.value.strip(), "city": self.city.value.strip(),
                "state": self.state.value.strip(), "zip": self.zip.value.strip(),
                "country": self.country.value.strip()}
        if self.kind == "shipping":
            self.t.shipping = data
            # Ask about billing next.
            await edit_to_next(interaction,
                               cfg("text", "forms", "billing", "prompt"),
                               BillingChoiceView(self.t), color_name="primary_color")
        else:
            self.t.billing = data
            self.t.billing_same = False
            await show_confirmation(interaction, self.t)


# Tiny "Continue" views between forms ----------------------------------------
def _continue_button(label_default, callback, style=discord.ButtonStyle.primary,
                     emoji=None):
    btn = discord.ui.Button(label=label_default, style=style, emoji=emoji_or_none(emoji))
    btn.callback = callback
    return btn


class PersonalContinueView(discord.ui.View):
    def __init__(self, t):
        super().__init__(timeout=900); self.t = t
        self.add_item(_continue_button("Continue ➜", self.go))
    async def go(self, interaction):
        await interaction.response.send_modal(PersonalModal(self.t))


class ShippingContinueView(discord.ui.View):
    def __init__(self, t):
        super().__init__(timeout=900); self.t = t
        self.add_item(_continue_button("Continue ➜", self.go))
    async def go(self, interaction):
        await interaction.response.send_modal(AddressModal(self.t, "shipping"))


class BillingChoiceView(discord.ui.View):
    """Same-as-shipping shortcut OR enter a different billing address."""
    def __init__(self, t):
        super().__init__(timeout=900); self.t = t
        same = discord.ui.Button(
            label=cfg("text", "forms", "billing", "same_button", default="Same"),
            style=discord.ButtonStyle.success)
        diff = discord.ui.Button(
            label=cfg("text", "forms", "billing", "different_button", default="Different"),
            style=discord.ButtonStyle.secondary)
        same.callback = self.same; diff.callback = self.diff
        self.add_item(same); self.add_item(diff)

    async def same(self, interaction):
        self.t.billing = dict(self.t.shipping)
        self.t.billing_same = True
        await show_confirmation(interaction, self.t)

    async def diff(self, interaction):
        await interaction.response.send_modal(AddressModal(self.t, "billing"))


async def edit_to_next(interaction, heading, view, color_name="success_color"):
    """After a modal submit, update the single wizard message + show next button."""
    e = discord.Embed(title=heading, color=color(color_name),
                      description="Tap **Continue** to keep going.")
    await interaction.response.edit_message(embed=e, view=view)


# =============================================================================
# 7) CONFIRMATION
# =============================================================================
async def show_confirmation(interaction, t):
    await interaction.response.edit_message(
        embed=summary_embed(t, cfg("text", "confirmation", "title"),
                            color("success_color")),
        view=ConfirmView(t))


class ConfirmView(discord.ui.View):
    def __init__(self, t):
        super().__init__(timeout=900); self.t = t
        confirm = discord.ui.Button(
            label=cfg("text", "confirmation", "confirm_button", "label", default="Confirm"),
            emoji=emoji_or_none(cfg("text", "confirmation", "confirm_button", "emoji")),
            style=discord.ButtonStyle.success)
        cancel = discord.ui.Button(
            label=cfg("text", "confirmation", "cancel_button", "label", default="Cancel"),
            emoji=emoji_or_none(cfg("text", "confirmation", "cancel_button", "emoji")),
            style=discord.ButtonStyle.danger)
        confirm.callback = self.confirm; cancel.callback = self.cancel
        self.add_item(confirm); self.add_item(cancel)

    async def cancel(self, interaction):
        await interaction.response.edit_message(
            content=cfg("text", "confirmation", "cancelled_message"),
            embed=None, view=None)

    async def confirm(self, interaction):
        await interaction.response.defer()  # ack the button; creating a channel takes a moment
        channel, order_no = await create_ticket_channel(interaction, self.t)
        # DM the customer their order number + a jump link.
        await dm_customer(interaction.user, order_no, channel)
        msg = cfg("text", "confirmation", "created_message").format(
            channel=channel.mention, order_no=order_no)
        await interaction.edit_original_response(content=msg, embed=None, view=None)


# =============================================================================
# 8) TICKET CHANNEL + ORDER NUMBER + DM + LOOKUP
# =============================================================================
def ticket_embed(t, user, order_no):
    e = summary_embed(t, cfg("text", "ticket", "title").format(order_no=order_no),
                      color("primary_color"), order_no=order_no)
    notice = cfg("text", "ticket", "notice")
    if notice:
        e.description = notice.format(user=user.mention, order_no=order_no)
    e.set_footer(text=cfg("text", "ticket", "footer", default=""))
    return e


async def create_ticket_channel(interaction, t):
    guild = interaction.guild
    user = interaction.user
    order_no = order_number()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    staff_role_id = cfg("server", "staff_role_id", default=0)
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    category_id = cfg("server", "ticket_category_id", default=0)
    category = guild.get_channel(category_id) if category_id else None

    channel = await guild.create_text_channel(
        name=order_no.lower(),  # e.g. "ord-7k2q9x"  -> used by /lookup
        overwrites=overwrites,
        category=category if isinstance(category, discord.CategoryChannel) else None,
        # The topic stores the order number + owner so /lookup works after restarts.
        topic=f"{order_no} | owner:{user.id} | {t.product.get('label','')}",
    )
    await channel.send(content=user.mention, embed=ticket_embed(t, user, order_no),
                       view=TicketView())
    return channel, order_no


async def dm_customer(user, order_no, channel):
    try:
        e = discord.Embed(
            title=cfg("text", "dm", "title", default="Your Ticket"),
            description=cfg("text", "dm", "body", default="").format(
                order_no=order_no, link=channel.jump_url),
            color=color("primary_color"))
        await user.send(embed=e)
    except discord.Forbidden:
        pass  # user has DMs closed — that's fine, number is also in the channel


class TicketView(discord.ui.View):
    """Persistent buttons inside each ticket."""
    def __init__(self):
        super().__init__(timeout=None)
        close = discord.ui.Button(
            label=cfg("text", "ticket", "close_button", "label", default="Close"),
            emoji=emoji_or_none(cfg("text", "ticket", "close_button", "emoji")),
            style=discord.ButtonStyle.danger, custom_id="ticket_close")
        support = discord.ui.Button(
            label=cfg("text", "ticket", "support_button", "label", default="Support"),
            emoji=emoji_or_none(cfg("text", "ticket", "support_button", "emoji")),
            style=discord.ButtonStyle.secondary, custom_id="ticket_support")
        close.callback = self.close; support.callback = self.support
        self.add_item(close); self.add_item(support)

    async def support(self, interaction):
        staff_role_id = cfg("server", "staff_role_id", default=0)
        role = interaction.guild.get_role(staff_role_id) if staff_role_id else None
        mention = role.mention if role else "@staff"
        await interaction.response.send_message(
            cfg("text", "ticket", "support_message").format(staff=mention))

    async def close(self, interaction):
        staff_role_id = cfg("server", "staff_role_id", default=0)
        has_role = any(r.id == staff_role_id for r in getattr(interaction.user, "roles", []))
        if not (has_role or interaction.user.guild_permissions.manage_channels):
            await interaction.response.send_message(
                cfg("text", "errors", "no_permission"), ephemeral=True); return
        await interaction.response.send_message(cfg("text", "ticket", "closing_message"))
        await asyncio.sleep(5)
        await interaction.channel.delete()


# =============================================================================
# 9) PANEL + COMMANDS
# =============================================================================
def panel_embed():
    e = discord.Embed(title=cfg("text", "panel", "title", default="Support"),
                      description=cfg("text", "panel", "description", default=""),
                      color=color("primary_color"))
    if cfg("text", "panel", "footer"):
        e.set_footer(text=cfg("text", "panel", "footer"))
    return e


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        link_url = cfg("text", "panel", "link_button", "url", default="")
        if link_url:
            self.add_item(discord.ui.Button(
                label=cfg("text", "panel", "link_button", "label", default="Link"),
                emoji=emoji_or_none(cfg("text", "panel", "link_button", "emoji")),
                url=link_url, style=discord.ButtonStyle.link))
        start = discord.ui.Button(
            label=cfg("text", "panel", "start_button", "label", default="Open Ticket"),
            emoji=emoji_or_none(cfg("text", "panel", "start_button", "emoji")),
            style=discord.ButtonStyle.primary, custom_id="panel_start")
        start.callback = self.start
        self.add_item(start)

    async def start(self, interaction):
        t = Ticket()
        await interaction.response.send_message(
            embed=step_embed(cfg("text", "steps", "choose_region"), 1, 3),
            view=RegionView(t), ephemeral=True)


class SupportBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        self.add_view(PanelView())
        self.add_view(TicketView())
        guild_id = cfg("server", "guild_id", default=0)
        if guild_id:
            g = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()


bot = SupportBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id}). Ready.")


@bot.tree.command(name="setup", description="Post the support panel.")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction):
    channel_id = cfg("server", "tickets_channel_id", default=0)
    target = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel
    await target.send(embed=panel_embed(), view=PanelView())
    await interaction.response.send_message(f"✅ Panel posted in {target.mention}.",
                                            ephemeral=True)


@setup.error
async def setup_error(interaction, error):
    await interaction.response.send_message(cfg("text", "errors", "no_permission"),
                                            ephemeral=True)


@bot.tree.command(name="lookup", description=cfg("text", "lookup", "description",
                                                 default="Find a ticket by order number."))
@app_commands.describe(order_number="Your order number, e.g. ORD-7K2Q9X")
async def lookup(interaction, order_number: str):
    """Finds a ticket by its order number, stored in the channel name/topic."""
    query = order_number.strip().upper()
    is_staff = False
    staff_role_id = cfg("server", "staff_role_id", default=0)
    if staff_role_id:
        is_staff = any(r.id == staff_role_id for r in getattr(interaction.user, "roles", []))
    is_staff = is_staff or interaction.user.guild_permissions.manage_channels

    for channel in interaction.guild.text_channels:
        topic = (channel.topic or "").upper()
        if query in channel.name.upper() or query in topic:
            # Only reveal it to the owner or staff.
            owner_ok = f"OWNER:{interaction.user.id}".upper() in topic
            if owner_ok or is_staff:
                await interaction.response.send_message(
                    cfg("text", "lookup", "found").format(
                        channel=channel.mention, order_no=query),
                    ephemeral=True)
                return
            break
    await interaction.response.send_message(
        cfg("text", "lookup", "not_found").format(order_no=query), ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
