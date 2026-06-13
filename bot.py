"""
=============================================================================
 STORE SUPPORT / ORDER-TRACKER BOT  (discord.py)
=============================================================================
 Customer flow:
   /setup posts a panel -> "Open a Support Ticket"
   -> pick STORE REGION -> ISSUE -> PRODUCT  (dropdowns, progress bars)
   -> see the SALE reveal -> "Open My Ticket"
   -> a private ticket opens with a UNIQUE ORDER NUMBER (DM'd to them)
   -> INSIDE the ticket they tap "Provide Order Details" and fill a short
      4-step form; the details get posted right into the ticket.

 Staff/owner commands (see /help):
   /close  /status  /setstatus  /rename  /add  /remove  /claim  /note
   /ticketinfo  /lookup  /help
   /credits view|add|remove|set|top

 No database for tickets: order number + context + status live in the channel
 name/topic, so everything survives restarts. Credits use a small JSON file
 (see README for making it persist on Railway).
=============================================================================
"""

import os
import json
import random
import string
import asyncio

import yaml
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# ---- Token + config ---------------------------------------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("ERROR: No DISCORD_TOKEN set (env var or .env file).")

with open("config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
CREDITS_FILE = os.path.join(DATA_DIR, "credits.json")


# ---- Helpers ----------------------------------------------------------------
def cfg(*keys, default=None):
    node = CONFIG
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return default
    return node


def color(name):
    return discord.Color(int(str(cfg("appearance", name, default="#5865F2")).lstrip("#"), 16))


def emoji_or_none(v):
    v = (v or "").strip()
    return v or None


def money(amount, symbol):
    if float(amount).is_integer():
        return f"{symbol}{int(amount):,}"
    return f"{symbol}{amount:,.2f}"


def progress_bar(n, total):
    return "▰" * n + "▱" * (total - n)


def order_number():
    return "ORD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def find_region(i): return next((r for r in cfg("regions", default=[]) if r["id"] == i), {})
def find_issue(i):  return next((x for x in cfg("issues", default=[]) if x["id"] == i), {})
def find_product(i):return next((p for p in cfg("products", default=[]) if p["id"] == i), {})
def find_status(i): return next((s for s in cfg("statuses", default=[]) if s["id"] == i), {})
def default_status(): return (cfg("statuses", default=[{}]) or [{}])[0]


def is_staff(interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    if perms and (perms.administrator or perms.manage_channels):
        return True
    sid = cfg("server", "staff_role_id", default=0)
    return bool(sid) and any(r.id == sid for r in getattr(interaction.user, "roles", []))


# ---- Tiny JSON store for credits -------------------------------------------
def _load_credits():
    try:
        with open(CREDITS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_credits(data):
    with open(CREDITS_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def get_credits(uid):
    return _load_credits().get(str(uid), cfg("credits", "starting_balance", default=0))


def change_credits(uid, delta):
    data = _load_credits()
    data[str(uid)] = round(data.get(str(uid), 0) + delta, 2)
    _save_credits(data)
    return data[str(uid)]


def set_credits(uid, value):
    data = _load_credits()
    data[str(uid)] = round(value, 2)
    _save_credits(data)
    return data[str(uid)]


# ---- Ticket context (rebuildable from the channel topic) -------------------
class Ctx:
    """Holds region/issue/product (+ runtime form data). Survives restarts via topic."""
    def __init__(self):
        self.region_id = self.issue_id = self.product_id = None
        self.order = self.personal = self.shipping = self.billing = {}
        self.billing_same = False
        self.channel = None
        self.prompt_message = None

    @property
    def region(self): return find_region(self.region_id)
    @property
    def issue(self): return find_issue(self.issue_id)
    @property
    def product(self): return find_product(self.product_id)
    @property
    def symbol(self): return self.region.get("symbol", "$")
    @property
    def base_price(self): return self.product.get("prices", {}).get(self.region_id, 0) or 0
    @property
    def discount_percent(self):
        return self.product.get("discount_percent", cfg("sale", "discount_percent", default=0))
    @property
    def deposit_percent(self): return cfg("sale", "deposit_percent", default=0)
    @property
    def final_total(self): return self.base_price * (100 - self.discount_percent) / 100
    @property
    def deposit(self): return self.base_price * self.deposit_percent / 100
    @property
    def savings(self): return self.base_price - self.final_total


def make_topic(order_no, owner_id, ctx, status_id):
    return (f"{order_no} | owner:{owner_id} | region:{ctx.region_id} | "
            f"issue:{ctx.issue_id} | product:{ctx.product_id} | status:{status_id}")


def parse_topic(channel):
    """Returns dict with order_no, owner, region/issue/product ids, status."""
    out = {"order_no": None, "owner": None, "region": None,
           "issue": None, "product": None, "status": None}
    topic = channel.topic or ""
    if topic and topic.split(" | ")[0].startswith("ORD-"):
        out["order_no"] = topic.split(" | ")[0].strip()
    for part in topic.split("|"):
        part = part.strip()
        for key in ("owner", "region", "issue", "product", "status"):
            if part.startswith(key + ":"):
                out[key] = part.split(":", 1)[1].strip()
    return out


def ctx_from_topic(channel):
    info = parse_topic(channel)
    c = Ctx()
    c.region_id, c.issue_id, c.product_id = info["region"], info["issue"], info["product"]
    c.channel = channel
    return c, info


# ---- Embeds -----------------------------------------------------------------
def step_embed(prompt, n, total):
    e = discord.Embed(title=cfg("text", "steps", "title", default="New Ticket"),
                      description=prompt, color=color("primary_color"))
    e.set_footer(text=cfg("text", "steps", "progress", default="{bar} {n}/{total}")
                 .format(bar=progress_bar(n, total), n=n, total=total))
    return e


def sale_embed(c):
    e = discord.Embed(title=cfg("text", "sale", "title", default="Your Deal"),
                      color=color("sale_color"))
    lines = [
        cfg("text", "sale", "line_was").format(base=money(c.base_price, c.symbol)),
        cfg("text", "sale", "line_off").format(percent=c.discount_percent),
        cfg("text", "sale", "line_total").format(final=money(c.final_total, c.symbol)),
        cfg("text", "sale", "line_deposit").format(deposit=money(c.deposit, c.symbol)),
        cfg("text", "sale", "line_savings").format(savings=money(c.savings, c.symbol)),
    ]
    e.description = (f"{c.product.get('emoji','')} **{c.product.get('label','')}** · "
                     f"{c.region.get('label','')}\n\n" + "\n".join(lines))
    return e


def deal_field_value(c):
    if not (cfg("sale", "enabled", default=True) and c.base_price):
        return None
    return (f"~~{money(c.base_price, c.symbol)}~~ → **{money(c.final_total, c.symbol)}** "
            f"({c.discount_percent}% off)\n💳 Due upfront: **{money(c.deposit, c.symbol)}**")


def welcome_embed(c, user, order_no, status):
    e = discord.Embed(
        title=cfg("text", "details", "welcome_title", default="Welcome").format(order_no=order_no),
        description=cfg("text", "details", "welcome_body", default="").format(
            user=user.mention, order_no=order_no),
        color=color("primary_color"))
    e.add_field(name="🏬 Store", value=f"{c.region.get('label','?')}\n`{c.region.get('domain','')}`", inline=True)
    e.add_field(name="🛠️ Issue", value=f"{c.issue.get('emoji','')} {c.issue.get('label','?')}", inline=True)
    e.add_field(name="📦 Product", value=f"{c.product.get('emoji','')} {c.product.get('label','?')}", inline=True)
    dv = deal_field_value(c)
    if dv:
        e.add_field(name="💰 Your Deal", value=dv, inline=False)
    e.add_field(name="📌 Status", value=f"{status.get('emoji','')} {status.get('label','')}", inline=False)
    e.set_footer(text=cfg("text", "ticket", "footer", default=""))
    return e


def address_block(a):
    if not a:
        return "—"
    parts = [a.get("street", ""),
             f"{a.get('city','')}, {a.get('state','')} {a.get('zip','')}".strip(", "),
             a.get("country", "")]
    return "\n".join(p for p in parts if p.strip())


def details_embed(c):
    e = discord.Embed(title=cfg("text", "details", "posted_title", default="Order Details"),
                      color=color("info_color"))
    o, p = c.order, c.personal
    e.add_field(name="📋 Order Information",
                value=(f"**Order #:** {o.get('order_number','—')}\n"
                       f"**Total:** {o.get('order_total','—')}\n"
                       f"**Ordered:** {o.get('order_date','—')}\n"
                       f"**Delivery:** {o.get('delivery_date') or '—'}\n"
                       f"**Items:** {o.get('items','—')}"), inline=False)
    e.add_field(name="👤 Your Details",
                value=(f"**Name:** {p.get('name','—')}\n"
                       f"**Email:** {p.get('email') or '—'}\n"
                       f"**Phone:** {p.get('phone','—')}"), inline=False)
    e.add_field(name="📦 Shipping", value=address_block(c.shipping), inline=True)
    e.add_field(name="🧾 Billing",
                value="Same as shipping" if c.billing_same else address_block(c.billing),
                inline=True)
    if p.get("notes"):
        e.add_field(name="📝 Other Details", value=p["notes"], inline=False)
    return e


def stepscreen(done, current_header):
    """A bold, obvious 'you're on the right page' progress screen between forms."""
    labels = [cfg("text", "forms", k, "header", default=k.upper())
              for k in ("order", "personal", "shipping", "billing")]
    lines = []
    for idx, lab in enumerate(labels):
        if idx < done:
            lines.append(f"✅ {lab}")
        elif idx == done:
            lines.append(f"➡️ **{lab}**")
        else:
            lines.append(f"⬜ {lab}")
    bar = progress_bar(done, len(labels))
    e = discord.Embed(
        title=f"📝 STEP {done} OF {len(labels)} COMPLETE",
        description=("**" + cfg("text", "details", "saved_step", default="Saved!") + "**\n\n"
                     + "\n".join(lines) + f"\n\n`{bar}`"),
        color=color("success_color"))
    return e


# =============================================================================
# DROPDOWN STEPS (region -> issue -> product -> sale)
# =============================================================================
class RegionSelect(discord.ui.Select):
    def __init__(self, c):
        self.c = c
        opts = [discord.SelectOption(label=r["label"], value=r["id"],
                emoji=emoji_or_none(r.get("emoji"))) for r in cfg("regions", default=[])]
        super().__init__(placeholder=cfg("text", "steps", "choose_region"), options=opts)
    async def callback(self, i):
        self.c.region_id = self.values[0]
        await i.response.edit_message(embed=step_embed(cfg("text", "steps", "choose_issue"), 2, 3),
                                      view=IssueView(self.c))


class IssueSelect(discord.ui.Select):
    def __init__(self, c):
        self.c = c
        opts = [discord.SelectOption(label=x["label"], value=x["id"],
                emoji=emoji_or_none(x.get("emoji"))) for x in cfg("issues", default=[])]
        super().__init__(placeholder=cfg("text", "steps", "choose_issue"), options=opts)
    async def callback(self, i):
        self.c.issue_id = self.values[0]
        await i.response.edit_message(embed=step_embed(cfg("text", "steps", "choose_product"), 3, 3),
                                      view=ProductView(self.c))


class ProductSelect(discord.ui.Select):
    def __init__(self, c):
        self.c = c
        opts = []
        for p in cfg("products", default=[]):
            price = p.get("prices", {}).get(c.region_id)
            opts.append(discord.SelectOption(
                label=p["label"], value=p["id"],
                description=money(price, c.symbol) if price is not None else None,
                emoji=emoji_or_none(p.get("emoji"))))
        super().__init__(placeholder=cfg("text", "steps", "choose_product"), options=opts)
    async def callback(self, i):
        self.c.product_id = self.values[0]
        if cfg("sale", "enabled", default=True) and self.c.base_price:
            await i.response.edit_message(embed=sale_embed(self.c), view=OpenTicketView(self.c))
        else:
            await open_ticket(i, self.c)


class RegionView(discord.ui.View):
    def __init__(self, c): super().__init__(timeout=900); self.add_item(RegionSelect(c))
class IssueView(discord.ui.View):
    def __init__(self, c): super().__init__(timeout=900); self.add_item(IssueSelect(c))
class ProductView(discord.ui.View):
    def __init__(self, c): super().__init__(timeout=900); self.add_item(ProductSelect(c))


class OpenTicketView(discord.ui.View):
    def __init__(self, c):
        super().__init__(timeout=900); self.c = c
        b = discord.ui.Button(
            label=cfg("text", "sale", "open_button", "label", default="Open My Ticket"),
            emoji=emoji_or_none(cfg("text", "sale", "open_button", "emoji")),
            style=discord.ButtonStyle.success)
        b.callback = self.go
        self.add_item(b)
    async def go(self, i):
        await open_ticket(i, self.c)


# =============================================================================
# OPEN THE TICKET CHANNEL
# =============================================================================
async def open_ticket(interaction, c):
    await interaction.response.defer()
    guild, user = interaction.guild, interaction.user
    order_no = order_number()
    status = default_status()

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    sid = cfg("server", "staff_role_id", default=0)
    role = guild.get_role(sid) if sid else None
    if role:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    cat_id = cfg("server", "ticket_category_id", default=0)
    category = guild.get_channel(cat_id) if cat_id else None

    channel = await guild.create_text_channel(
        name=f"{status.get('emoji','')}{order_no.lower()}",
        overwrites=overwrites,
        category=category if isinstance(category, discord.CategoryChannel) else None,
        topic=make_topic(order_no, user.id, c, status.get("id", "open")))

    prompt = await channel.send(embed=welcome_embed(c, user, order_no, status),
                                view=DetailsStartView())
    await channel.send(view=TicketView())  # quick-action buttons (close/support)

    # DM the order number
    try:
        e = discord.Embed(title=cfg("text", "dm", "title", default="Your Ticket"),
                          description=cfg("text", "dm", "body", default="").format(
                              order_no=order_no, link=channel.jump_url),
                          color=color("primary_color"))
        await user.send(embed=e)
    except discord.Forbidden:
        pass

    await interaction.edit_original_response(
        content=f"🎉 Your ticket is open: {channel.mention}\nYour order number is **{order_no}**.",
        embed=None, view=None)


# =============================================================================
# IN-TICKET: "Provide Order Details" -> chained 4-step form
# =============================================================================
class DetailsStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        b = discord.ui.Button(
            label=cfg("text", "details", "provide_button", "label", default="Provide Order Details"),
            emoji=emoji_or_none(cfg("text", "details", "provide_button", "emoji")),
            style=discord.ButtonStyle.primary, custom_id="details_start")
        b.callback = self.start
        self.add_item(b)

    async def start(self, interaction):
        info = parse_topic(interaction.channel)
        # Only the ticket owner or staff may fill details.
        if str(interaction.user.id) != str(info.get("owner")) and not is_staff(interaction):
            await interaction.response.send_message(cfg("text", "errors", "no_permission"),
                                                    ephemeral=True); return
        c, _ = ctx_from_topic(interaction.channel)
        c.prompt_message = interaction.message
        await interaction.response.send_modal(OrderModal(c))


def ti(label, ph="", required=True, style=discord.TextStyle.short, max_length=300):
    return discord.ui.TextInput(label=label[:45], placeholder=ph[:100],
                                required=required, style=style, max_length=max_length)


class OrderModal(discord.ui.Modal):
    def __init__(self, c):
        super().__init__(title=cfg("text", "forms", "order", "title")[:45]); self.c = c
        g = lambda k: cfg("text", "forms", "order", k, default=k)
        self.order_number = ti(g("order_number"), g("order_number_ph"))
        self.order_total = ti(g("order_total"), g("order_total_ph"))
        self.order_date = ti(g("order_date"), g("order_date_ph"))
        self.delivery_date = ti(g("delivery_date"), g("delivery_date_ph"), required=False)
        self.items = ti(g("items"), g("items_ph"), style=discord.TextStyle.paragraph, max_length=600)
        for x in (self.order_number, self.order_total, self.order_date, self.delivery_date, self.items):
            self.add_item(x)
    async def on_submit(self, i):
        self.c.order = {"order_number": self.order_number.value.strip(),
                        "order_total": self.order_total.value.strip(),
                        "order_date": self.order_date.value.strip(),
                        "delivery_date": self.delivery_date.value.strip(),
                        "items": self.items.value.strip()}
        await i.response.send_message(embed=stepscreen(1, "personal"),
                                      view=ContinueView(self.c, PersonalModal), ephemeral=True)


class PersonalModal(discord.ui.Modal):
    def __init__(self, c):
        super().__init__(title=cfg("text", "forms", "personal", "title")[:45]); self.c = c
        g = lambda k: cfg("text", "forms", "personal", k, default=k)
        self.name = ti(g("name"), g("name_ph"))
        self.email = ti(g("email"), g("email_ph"), required=False)
        self.phone = ti(g("phone"), g("phone_ph"))
        self.notes = ti(g("notes"), g("notes_ph"), required=False,
                        style=discord.TextStyle.paragraph, max_length=600)
        for x in (self.name, self.email, self.phone, self.notes):
            self.add_item(x)
    async def on_submit(self, i):
        self.c.personal = {"name": self.name.value.strip(), "email": self.email.value.strip(),
                           "phone": self.phone.value.strip(), "notes": self.notes.value.strip()}
        await i.response.edit_message(embed=stepscreen(2, "shipping"),
                                      view=ContinueView(self.c, lambda c: AddressModal(c, "shipping")))


class AddressModal(discord.ui.Modal):
    def __init__(self, c, kind):
        super().__init__(title=cfg("text", "forms", kind, "title")[:45]); self.c = c; self.kind = kind
        g = lambda k: cfg("text", "forms", kind, k, default=k)
        self.street = ti(g("street")); self.city = ti(g("city")); self.state = ti(g("state"))
        self.zip = ti(g("zip")); self.country = ti(g("country"))
        for x in (self.street, self.city, self.state, self.zip, self.country):
            self.add_item(x)
    async def on_submit(self, i):
        data = {"street": self.street.value.strip(), "city": self.city.value.strip(),
                "state": self.state.value.strip(), "zip": self.zip.value.strip(),
                "country": self.country.value.strip()}
        if self.kind == "shipping":
            self.c.shipping = data
            await i.response.edit_message(embed=stepscreen(3, "billing"), view=BillingChoiceView(self.c))
        else:
            self.c.billing = data; self.c.billing_same = False
            await finish_details(i, self.c)


class ContinueView(discord.ui.View):
    """A bold Continue button that opens the next modal."""
    def __init__(self, c, modal_factory):
        super().__init__(timeout=900); self.c = c; self.modal_factory = modal_factory
        b = discord.ui.Button(label="Continue ➜", style=discord.ButtonStyle.primary, emoji="✨")
        b.callback = self.go
        self.add_item(b)
    async def go(self, i):
        await i.response.send_modal(self.modal_factory(self.c))


class BillingChoiceView(discord.ui.View):
    def __init__(self, c):
        super().__init__(timeout=900); self.c = c
        same = discord.ui.Button(label=cfg("text", "forms", "billing", "same_button", default="Same"),
                                 style=discord.ButtonStyle.success)
        diff = discord.ui.Button(label=cfg("text", "forms", "billing", "different_button", default="Different"),
                                 style=discord.ButtonStyle.secondary)
        same.callback = self.same; diff.callback = self.diff
        self.add_item(same); self.add_item(diff)
    async def same(self, i):
        self.c.billing = dict(self.c.shipping); self.c.billing_same = True
        await finish_details(i, self.c)
    async def diff(self, i):
        await i.response.send_modal(AddressModal(self.c, "billing"))


async def finish_details(interaction, c):
    # Post the details into the ticket and confirm to the user.
    await c.channel.send(content=f"📋 Details submitted by {interaction.user.mention}:",
                         embed=details_embed(c))
    try:
        if c.prompt_message:
            await c.prompt_message.edit(view=DetailsStartView())  # keep button for updates
    except discord.HTTPException:
        pass
    await interaction.response.edit_message(
        content=cfg("text", "details", "all_done", default="✅ Done!"),
        embed=None, view=None)


# =============================================================================
# TICKET QUICK-ACTION BUTTONS
# =============================================================================
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        close = discord.ui.Button(label=cfg("text", "ticket", "close_button", "label", default="Close"),
                                  emoji=emoji_or_none(cfg("text", "ticket", "close_button", "emoji")),
                                  style=discord.ButtonStyle.danger, custom_id="ticket_close")
        support = discord.ui.Button(label=cfg("text", "ticket", "support_button", "label", default="Support"),
                                    emoji=emoji_or_none(cfg("text", "ticket", "support_button", "emoji")),
                                    style=discord.ButtonStyle.secondary, custom_id="ticket_support")
        close.callback = self.close; support.callback = self.support
        self.add_item(close); self.add_item(support)
    async def support(self, i):
        sid = cfg("server", "staff_role_id", default=0)
        role = i.guild.get_role(sid) if sid else None
        await i.response.send_message(cfg("text", "ticket", "support_message").format(
            staff=role.mention if role else "@staff"))
    async def close(self, i):
        if not is_staff(i):
            await i.response.send_message(cfg("text", "errors", "no_permission"), ephemeral=True); return
        await i.response.send_message(cfg("text", "ticket", "closing_message"))
        await asyncio.sleep(5)
        await i.channel.delete()


# =============================================================================
# PANEL
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
        url = cfg("text", "panel", "link_button", "url", default="")
        if url:
            self.add_item(discord.ui.Button(
                label=cfg("text", "panel", "link_button", "label", default="Link"),
                emoji=emoji_or_none(cfg("text", "panel", "link_button", "emoji")),
                url=url, style=discord.ButtonStyle.link))
        b = discord.ui.Button(label=cfg("text", "panel", "start_button", "label", default="Open Ticket"),
                              emoji=emoji_or_none(cfg("text", "panel", "start_button", "emoji")),
                              style=discord.ButtonStyle.primary, custom_id="panel_start")
        b.callback = self.start
        self.add_item(b)
    async def start(self, i):
        await i.response.send_message(embed=step_embed(cfg("text", "steps", "choose_region"), 1, 3),
                                      view=RegionView(Ctx()), ephemeral=True)


# =============================================================================
# BOT + COMMANDS
# =============================================================================
class SupportBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())
    async def setup_hook(self):
        self.add_view(PanelView()); self.add_view(TicketView()); self.add_view(DetailsStartView())
        gid = cfg("server", "guild_id", default=0)
        if gid:
            g = discord.Object(id=gid); self.tree.copy_global_to(guild=g); await self.tree.sync(guild=g)
        else:
            await self.tree.sync()


bot = SupportBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id}). Ready.")


# ----- helper: rename channel keeping the order number ----------------------
async def apply_status(channel, status, mod, *, announce=True, dm=True):
    info = parse_topic(channel)
    order_no = info.get("order_no") or channel.name.upper()
    # Update topic
    new_topic = channel.topic or ""
    if "status:" in new_topic:
        new_topic = " | ".join(
            (f"status:{status['id']}" if p.strip().startswith("status:") else p.strip())
            for p in new_topic.split("|"))
    else:
        new_topic = f"{new_topic} | status:{status['id']}"
    # Rename channel: <emoji><order-no>
    try:
        await channel.edit(name=f"{status.get('emoji','')}{order_no.lower()}", topic=new_topic)
    except discord.HTTPException:
        await channel.edit(topic=new_topic)
    if announce:
        await channel.send(cfg("text", "status", "changed_channel").format(
            emoji=status.get("emoji", ""), label=status.get("label", ""), mod=mod.mention))
    if dm and info.get("owner") and str(info["owner"]).isdigit():
        try:
            member = await bot.fetch_user(int(info["owner"]))
            e = discord.Embed(
                title=cfg("text", "status", "dm_title", default="Update").format(order_no=order_no),
                description=cfg("text", "status", "dm_body", default="").format(
                    order_no=order_no, emoji=status.get("emoji", ""), label=status.get("label", "")),
                color=color("info_color"))
            await member.send(embed=e)
        except (discord.Forbidden, discord.HTTPException):
            pass


# ----- /setup ----------------------------------------------------------------
@bot.tree.command(name="setup", description="Post the support panel. (Admin)")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction):
    cid = cfg("server", "tickets_channel_id", default=0)
    target = interaction.guild.get_channel(cid) if cid else interaction.channel
    await target.send(embed=panel_embed(), view=PanelView())
    await interaction.response.send_message(f"✅ Panel posted in {target.mention}.", ephemeral=True)

@setup.error
async def setup_error(interaction, error):
    await interaction.response.send_message(cfg("text", "errors", "no_permission"), ephemeral=True)


# ----- /lookup ---------------------------------------------------------------
@bot.tree.command(name="lookup", description="Find your ticket by order number.")
@app_commands.describe(order_number="e.g. ORD-7K2Q9X")
async def lookup(interaction, order_number: str):
    q = order_number.strip().upper()
    staff = is_staff(interaction)
    for ch in interaction.guild.text_channels:
        if q in ch.name.upper() or q in (ch.topic or "").upper():
            owner_ok = f"OWNER:{interaction.user.id}" in (ch.topic or "").upper()
            if owner_ok or staff:
                await interaction.response.send_message(
                    cfg("text", "lookup", "found").format(channel=ch.mention, order_no=q),
                    ephemeral=True); return
            break
    await interaction.response.send_message(
        cfg("text", "lookup", "not_found").format(order_no=q), ephemeral=True)


# ----- /status (anyone in ticket) -------------------------------------------
@bot.tree.command(name="status", description="Show this ticket's current status.")
async def status_cmd(interaction):
    info = parse_topic(interaction.channel)
    if not info.get("order_no"):
        await interaction.response.send_message(cfg("text", "errors", "not_in_ticket"), ephemeral=True); return
    s = find_status(info.get("status")) or default_status()
    await interaction.response.send_message(
        cfg("text", "status", "current").format(emoji=s.get("emoji", ""), label=s.get("label", "")),
        ephemeral=True)


# ----- /setstatus (staff) ----------------------------------------------------
_status_choices = [app_commands.Choice(name=f"{s.get('emoji','')} {s['label']}", value=s["id"])
                   for s in cfg("statuses", default=[])]

@bot.tree.command(name="setstatus", description="Set this ticket's status. (Staff)")
@app_commands.describe(status="The new status")
@app_commands.choices(status=_status_choices)
async def setstatus(interaction, status: app_commands.Choice[str]):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    info = parse_topic(interaction.channel)
    if not info.get("order_no"):
        await interaction.response.send_message(cfg("text", "errors", "not_in_ticket"), ephemeral=True); return
    s = find_status(status.value)
    await interaction.response.send_message(f"✅ Status set to {s.get('emoji','')} **{s.get('label','')}**.",
                                            ephemeral=True)
    await apply_status(interaction.channel, s, interaction.user)


# ----- /close (staff) --------------------------------------------------------
@bot.tree.command(name="close", description="Close and delete this ticket. (Staff)")
@app_commands.describe(reason="Optional reason")
async def close_cmd(interaction, reason: str = ""):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    if not parse_topic(interaction.channel).get("order_no"):
        await interaction.response.send_message(cfg("text", "errors", "not_in_ticket"), ephemeral=True); return
    await interaction.response.send_message(
        f"{cfg('text','ticket','closing_message')}" + (f"\nReason: {reason}" if reason else ""))
    await asyncio.sleep(5)
    await interaction.channel.delete()


# ----- /rename (staff) -------------------------------------------------------
@bot.tree.command(name="rename", description="Rename this ticket. (Staff)")
@app_commands.describe(name="New name (order number stays in the topic)")
async def rename_cmd(interaction, name: str):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    await interaction.channel.edit(name=name)
    await interaction.response.send_message(f"✅ Renamed to **{name}**.", ephemeral=True)


# ----- /add and /remove user (staff) ----------------------------------------
@bot.tree.command(name="add", description="Add a user to this ticket. (Staff)")
@app_commands.describe(user="Who to add")
async def add_cmd(interaction, user: discord.Member):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    await interaction.channel.set_permissions(user, view_channel=True, send_messages=True)
    await interaction.response.send_message(f"✅ Added {user.mention} to the ticket.")

@bot.tree.command(name="remove", description="Remove a user from this ticket. (Staff)")
@app_commands.describe(user="Who to remove")
async def remove_cmd(interaction, user: discord.Member):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    await interaction.channel.set_permissions(user, overwrite=None)
    await interaction.response.send_message(f"✅ Removed {user.mention} from the ticket.")


# ----- /claim (staff) --------------------------------------------------------
@bot.tree.command(name="claim", description="Claim this ticket. (Staff)")
async def claim_cmd(interaction):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    s = find_status("in_progress") or default_status()
    await interaction.response.send_message(f"🙋 {interaction.user.mention} has claimed this ticket.")
    await apply_status(interaction.channel, s, interaction.user, announce=False)


# ----- /note (staff) ---------------------------------------------------------
@bot.tree.command(name="note", description="Post an internal staff note. (Staff)")
@app_commands.describe(text="The note")
async def note_cmd(interaction, text: str):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    e = discord.Embed(title="🗒️ Staff Note", description=text, color=color("info_color"))
    e.set_footer(text=f"by {interaction.user.display_name}")
    await interaction.channel.send(embed=e)
    await interaction.response.send_message("✅ Note posted.", ephemeral=True)


# ----- /ticketinfo -----------------------------------------------------------
@bot.tree.command(name="ticketinfo", description="Show this ticket's order context.")
async def ticketinfo_cmd(interaction):
    info = parse_topic(interaction.channel)
    if not info.get("order_no"):
        await interaction.response.send_message(cfg("text", "errors", "not_in_ticket"), ephemeral=True); return
    c, _ = ctx_from_topic(interaction.channel)
    s = find_status(info.get("status")) or default_status()
    owner = f"<@{info['owner']}>" if info.get("owner") else "—"
    e = discord.Embed(title=f"🎫 {info['order_no']}", color=color("primary_color"))
    e.add_field(name="Owner", value=owner, inline=True)
    e.add_field(name="Status", value=f"{s.get('emoji','')} {s.get('label','')}", inline=True)
    e.add_field(name="Store", value=c.region.get("label", "?"), inline=True)
    e.add_field(name="Issue", value=c.issue.get("label", "?"), inline=True)
    e.add_field(name="Product", value=c.product.get("label", "?"), inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


# ----- /credits group --------------------------------------------------------
credits_group = app_commands.Group(name="credits", description="Store credit commands")

@credits_group.command(name="view", description="View your (or someone's) credit balance.")
@app_commands.describe(user="Whose balance (staff only for others)")
async def credits_view(interaction, user: discord.Member = None):
    sym = cfg("credits", "symbol", default="$")
    if user and user.id != interaction.user.id:
        if not is_staff(interaction):
            await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
        bal = get_credits(user.id)
        await interaction.response.send_message(cfg("text", "credits_text", "balance_other").format(
            user=user.mention, symbol=sym, balance=bal), ephemeral=True)
    else:
        bal = get_credits(interaction.user.id)
        await interaction.response.send_message(cfg("text", "credits_text", "balance_self").format(
            symbol=sym, balance=bal), ephemeral=True)

@credits_group.command(name="add", description="Give credits to a user. (Staff)")
@app_commands.describe(user="Recipient", amount="Amount to add", reason="Optional reason")
async def credits_add(interaction, user: discord.Member, amount: float, reason: str = ""):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message(cfg("text", "errors", "bad_amount"), ephemeral=True); return
    sym = cfg("credits", "symbol", default="$")
    bal = change_credits(user.id, amount)
    await interaction.response.send_message(cfg("text", "credits_text", "added").format(
        user=user.mention, symbol=sym, amount=amount, balance=bal))
    try:
        rtxt = f"\nReason: {reason}" if reason else ""
        e = discord.Embed(title=cfg("text", "credits_text", "granted_dm_title", default="Credits!"),
                          description=cfg("text", "credits_text", "granted_dm_body").format(
                              symbol=sym, amount=amount, balance=bal, reason=rtxt),
                          color=color("success_color"))
        await user.send(embed=e)
    except discord.Forbidden:
        pass

@credits_group.command(name="remove", description="Remove credits from a user. (Staff)")
@app_commands.describe(user="User", amount="Amount to remove", reason="Optional reason")
async def credits_remove(interaction, user: discord.Member, amount: float, reason: str = ""):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message(cfg("text", "errors", "bad_amount"), ephemeral=True); return
    sym = cfg("credits", "symbol", default="$")
    bal = change_credits(user.id, -amount)
    await interaction.response.send_message(cfg("text", "credits_text", "removed").format(
        user=user.mention, symbol=sym, amount=amount, balance=bal))
    try:
        rtxt = f"\nReason: {reason}" if reason else ""
        await user.send(cfg("text", "credits_text", "removed_dm_body").format(
            symbol=sym, amount=amount, balance=bal, reason=rtxt))
    except discord.Forbidden:
        pass

@credits_group.command(name="set", description="Set a user's exact balance. (Staff)")
@app_commands.describe(user="User", amount="New balance")
async def credits_set(interaction, user: discord.Member, amount: float):
    if not is_staff(interaction):
        await interaction.response.send_message(cfg("text", "errors", "staff_only"), ephemeral=True); return
    sym = cfg("credits", "symbol", default="$")
    bal = set_credits(user.id, amount)
    await interaction.response.send_message(cfg("text", "credits_text", "set").format(
        user=user.mention, symbol=sym, balance=bal))

@credits_group.command(name="top", description="Show the credit leaderboard.")
async def credits_top(interaction):
    sym = cfg("credits", "symbol", default="$")
    data = _load_credits()
    rows = sorted(((uid, bal) for uid, bal in data.items() if bal), key=lambda x: x[1], reverse=True)[:10]
    if not rows:
        await interaction.response.send_message(cfg("text", "credits_text", "leaderboard_empty"),
                                                ephemeral=True); return
    lines = [f"**{n}.** <@{uid}> — {sym}{bal:,.2f}" for n, (uid, bal) in enumerate(rows, 1)]
    e = discord.Embed(title=cfg("text", "credits_text", "leaderboard_title", default="Leaderboard"),
                      description="\n".join(lines), color=color("sale_color"))
    await interaction.response.send_message(embed=e)

bot.tree.add_command(credits_group)


# ----- /help -----------------------------------------------------------------
@bot.tree.command(name="help", description="List the bot's commands.")
async def help_cmd(interaction):
    staff = is_staff(interaction)
    e = discord.Embed(title="📖 Commands", color=color("primary_color"))
    e.add_field(name="Everyone", value=(
        "`/lookup <order#>` — reopen your ticket\n"
        "`/status` — see this ticket's status\n"
        "`/credits view` — see your balance\n"
        "`/help` — this list"), inline=False)
    if staff:
        e.add_field(name="Staff / Owner", value=(
            "`/setup` — post the panel\n"
            "`/setstatus <status>` — update status (renames channel + DMs the customer)\n"
            "`/close [reason]` — close & delete the ticket\n"
            "`/rename <name>` · `/add <user>` · `/remove <user>`\n"
            "`/claim` — claim the ticket\n"
            "`/note <text>` — post a staff note\n"
            "`/ticketinfo` — show ticket context\n"
            "`/credits add|remove|set <user> <amount>` · `/credits top`"), inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
