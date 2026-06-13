"""
=============================================================================
 ORDER TICKET BOT  (discord.py)
=============================================================================
 A modular, config-driven Discord ticket bot.

 The FLOW:
   1. /setup posts a panel with a "Start an Order" button.
   2. User clicks it -> picks a COMPANY (dropdown).
   3. -> picks a REGION (dropdown, depends on the company).
   4. -> picks a PRODUCT (dropdown, depends on the company).
   5. -> fills in a short FORM (quantity + notes).
   6. -> sees a private CONFIRMATION with Confirm / Cancel buttons.
   7. On Confirm -> a private ticket CHANNEL is created with an info embed
      and "Close Ticket" / "Request Support" buttons.

 You normally never need to edit this file. All text, colors, companies,
 products, prices, channels and roles live in config.yaml.
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


# -----------------------------------------------------------------------------
# 1) Load the secret token and the config file
# -----------------------------------------------------------------------------
load_dotenv()  # reads a local .env file if present (ignored on hosts that
               # already set environment variables for you)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit(
        "ERROR: No DISCORD_TOKEN found.\n"
        "Locally: create a .env file (copy .env.example) and paste your token.\n"
        "On a host: set DISCORD_TOKEN as an environment variable / 'secret'."
    )


def load_config():
    """Read config.yaml. Done in a function so it's easy to reload if needed."""
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG = load_config()


# -----------------------------------------------------------------------------
# 2) Small helpers for reading config cleanly
# -----------------------------------------------------------------------------
def cfg(*keys, default=None):
    """Safely dig into nested config, e.g. cfg('text', 'panel', 'title')."""
    node = CONFIG
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return default
    return node


def color(name):
    """Turn a hex string from config (e.g. '#5865F2') into a discord.Color."""
    hexstr = cfg("appearance", name, default="#5865F2")
    return discord.Color(int(str(hexstr).lstrip("#"), 16))


def emoji_or_none(value):
    """Discord wants None (not an empty string) when there is no emoji."""
    value = (value or "").strip()
    return value or None


def format_price(amount, symbol):
    """Pretty-print a price: whole numbers show no decimals, otherwise 2dp."""
    if float(amount).is_integer():
        return f"{symbol}{int(amount):,}"
    return f"{symbol}{amount:,.2f}"


def random_code(length=6):
    """Short random id for ticket channel names, e.g. 'order-7gk2qa'."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


# -----------------------------------------------------------------------------
# 3) Embed builders (so every embed is created the same consistent way)
# -----------------------------------------------------------------------------
def panel_embed():
    e = discord.Embed(
        title=cfg("text", "panel", "title", default="Order Desk"),
        description=cfg("text", "panel", "description", default=""),
        color=color("primary_color"),
    )
    footer = cfg("text", "panel", "footer")
    if footer:
        e.set_footer(text=footer)
    return e


def step_embed(description):
    return discord.Embed(
        title=cfg("text", "steps", "title", default="New Order"),
        description=description,
        color=color("primary_color"),
    )


# -----------------------------------------------------------------------------
# 4) The order "state" we carry through the steps
# -----------------------------------------------------------------------------
class OrderState:
    """Holds the choices a user makes as they move through the flow."""
    def __init__(self):
        self.company_id = None
        self.region_id = None
        self.product_id = None
        self.quantity = 1
        self.notes = ""

    # Convenience lookups into the config -------------------------------------
    @property
    def company(self):
        return cfg("companies", self.company_id, default={})

    @property
    def region(self):
        for r in self.company.get("regions", []):
            if r["id"] == self.region_id:
                return r
        return {}

    @property
    def product(self):
        for p in self.company.get("products", []):
            if p["id"] == self.product_id:
                return p
        return {}

    @property
    def unit_price(self):
        return self.product.get("prices", {}).get(self.region_id, 0)

    @property
    def fee(self):
        return self.company.get("fee", 0) or 0

    @property
    def total(self):
        return self.unit_price * self.quantity + self.fee

    @property
    def symbol(self):
        return self.region.get("symbol", "$")


# =============================================================================
# 5) THE STEP-BY-STEP VIEWS (dropdowns + form)
#    These are short-lived: they appear in the user's private (ephemeral)
#    message and update in place as the user makes each choice.
# =============================================================================

class CompanySelect(discord.ui.Select):
    """Step 1: choose a company."""
    def __init__(self, state):
        self.state = state
        options = [
            discord.SelectOption(
                label=c["label"], value=cid, emoji=emoji_or_none(c.get("emoji"))
            )
            for cid, c in cfg("companies", default={}).items()
        ]
        super().__init__(
            placeholder=cfg("text", "steps", "choose_company", default="Choose…"),
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.company_id = self.values[0]
        # Move on to the region step by replacing the dropdown.
        await interaction.response.edit_message(
            embed=step_embed(cfg("text", "steps", "choose_region")),
            view=RegionView(self.state),
        )


class RegionSelect(discord.ui.Select):
    """Step 2: choose a region (options depend on the chosen company)."""
    def __init__(self, state):
        self.state = state
        options = [
            discord.SelectOption(
                label=r["label"], value=r["id"], emoji=emoji_or_none(r.get("emoji"))
            )
            for r in state.company.get("regions", [])
        ]
        super().__init__(
            placeholder=cfg("text", "steps", "choose_region", default="Choose…"),
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.region_id = self.values[0]
        await interaction.response.edit_message(
            embed=step_embed(cfg("text", "steps", "choose_product")),
            view=ProductView(self.state),
        )


class ProductSelect(discord.ui.Select):
    """Step 3: choose a product. After choosing, we open the details form."""
    def __init__(self, state):
        self.state = state
        options = []
        for p in state.company.get("products", []):
            price = p.get("prices", {}).get(state.region_id)
            # Show the price for the chosen region right in the dropdown.
            desc = format_price(price, state.symbol) if price is not None else None
            options.append(discord.SelectOption(
                label=p["label"], value=p["id"],
                description=desc, emoji=emoji_or_none(p.get("emoji")),
            ))
        super().__init__(
            placeholder=cfg("text", "steps", "choose_product", default="Choose…"),
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.state.product_id = self.values[0]
        # A select callback is allowed to open a modal (form) directly.
        await interaction.response.send_modal(OrderModal(self.state))


# Tiny wrapper Views so each Select sits inside its own message --------------
class CompanyView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=600)
        self.add_item(CompanySelect(state))

class RegionView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=600)
        self.add_item(RegionSelect(state))

class ProductView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=600)
        self.add_item(ProductSelect(state))


# -----------------------------------------------------------------------------
# 6) The order form (modal) — quantity + notes
# -----------------------------------------------------------------------------
class OrderModal(discord.ui.Modal):
    def __init__(self, state):
        super().__init__(title=cfg("text", "modal", "title", default="Order Details"))
        self.state = state

        # Discord shows a built-in "This field is required" error when a
        # required field is left blank, so required-field validation is free.
        self.quantity = discord.ui.TextInput(
            label=cfg("text", "modal", "quantity_label", default="Quantity"),
            placeholder=cfg("text", "modal", "quantity_placeholder", default="1"),
            required=True, max_length=6,
        )
        self.notes = discord.ui.TextInput(
            label=cfg("text", "modal", "notes_label", default="Notes"),
            placeholder=cfg("text", "modal", "notes_placeholder", default=""),
            required=False, style=discord.TextStyle.paragraph, max_length=500,
        )
        self.add_item(self.quantity)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        # Validate that quantity is a positive whole number.
        raw = self.quantity.value.strip()
        if not raw.isdigit() or int(raw) <= 0:
            # Re-offer the form via a "Try Again" button.
            await interaction.response.send_message(
                cfg("text", "errors", "invalid_quantity"),
                view=RetryView(self.state), ephemeral=True,
            )
            return

        self.state.quantity = int(raw)
        self.state.notes = self.notes.value.strip()

        # Show the confirmation summary (only the buyer can see it).
        await interaction.response.send_message(
            embed=confirmation_embed(self.state),
            view=ConfirmView(self.state), ephemeral=True,
        )


class RetryView(discord.ui.View):
    """Shown if the quantity was invalid — lets the user reopen the form."""
    def __init__(self, state):
        super().__init__(timeout=600)
        self.state = state

    @discord.ui.button(label="Try Again", style=discord.ButtonStyle.primary)
    async def try_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OrderModal(self.state))


# -----------------------------------------------------------------------------
# 7) Confirmation step
# -----------------------------------------------------------------------------
def confirmation_embed(state):
    e = discord.Embed(
        title=cfg("text", "confirmation", "title", default="Order Confirmation"),
        description=cfg("text", "confirmation", "description", default=""),
        color=color("success_color"),
    )
    e.add_field(name="Company", value=state.company.get("label", "?"), inline=True)
    e.add_field(name="Region", value=state.region.get("label", "?"), inline=True)
    e.add_field(name="Product", value=state.product.get("label", "?"), inline=True)
    e.add_field(name="Quantity", value=str(state.quantity), inline=True)
    e.add_field(name="Unit Price",
                value=format_price(state.unit_price, state.symbol), inline=True)
    if state.fee:
        e.add_field(name="Fee", value=format_price(state.fee, state.symbol), inline=True)
    e.add_field(name="Order Total",
                value=format_price(state.total, state.symbol), inline=False)
    if state.notes:
        e.add_field(name="Notes", value=state.notes, inline=False)
    footer = cfg("text", "confirmation", "footer")
    if footer:
        e.set_footer(text=footer)
    return e


class ConfirmView(discord.ui.View):
    def __init__(self, state):
        super().__init__(timeout=600)
        self.state = state

        confirm = discord.ui.Button(
            label=cfg("text", "confirmation", "confirm_button", "label", default="Confirm"),
            emoji=emoji_or_none(cfg("text", "confirmation", "confirm_button", "emoji")),
            style=discord.ButtonStyle.success,
        )
        cancel = discord.ui.Button(
            label=cfg("text", "confirmation", "cancel_button", "label", default="Cancel"),
            emoji=emoji_or_none(cfg("text", "confirmation", "cancel_button", "emoji")),
            style=discord.ButtonStyle.danger,
        )
        confirm.callback = self.on_confirm
        cancel.callback = self.on_cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def on_cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=cfg("text", "confirmation", "cancelled_message"),
            embed=None, view=None,
        )

    async def on_confirm(self, interaction: discord.Interaction):
        channel = await create_ticket_channel(interaction, self.state)
        msg = cfg("text", "confirmation", "created_message",
                  default="Ticket created: {channel}")
        await interaction.response.edit_message(
            content=msg.format(channel=channel.mention),
            embed=None, view=None,
        )


# -----------------------------------------------------------------------------
# 8) Creating the private ticket channel
# -----------------------------------------------------------------------------
def ticket_info_embed(state, user):
    e = discord.Embed(
        title=cfg("text", "ticket", "title", default="Order Information"),
        color=color("primary_color"),
    )
    flow = (
        f"{state.company.get('emoji','')} **{state.company.get('label','?')}** "
        f"({state.region.get('label','?')})  ➜  "
        f"{state.product.get('emoji','')} **{state.product.get('label','?')}**"
    )
    e.add_field(name="Order", value=flow, inline=False)
    e.add_field(name="Quantity", value=str(state.quantity), inline=True)
    e.add_field(name="Unit Price",
                value=format_price(state.unit_price, state.symbol), inline=True)
    if state.fee:
        e.add_field(name="Fee", value=format_price(state.fee, state.symbol), inline=True)
    e.add_field(name="Order Total",
                value=format_price(state.total, state.symbol), inline=False)
    if state.notes:
        e.add_field(name="Notes", value=state.notes, inline=False)

    notice = cfg("text", "ticket", "notice", default="")
    if notice:
        e.add_field(name="\u200b", value=notice.format(user=user.mention), inline=False)

    footer = cfg("text", "ticket", "footer")
    if footer:
        e.set_footer(text=footer)
    return e


async def create_ticket_channel(interaction: discord.Interaction, state):
    guild = interaction.guild
    user = interaction.user

    # Who can see the new private channel?
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    staff_role_id = cfg("server", "staff_role_id", default=0)
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True
        )

    # Optional category to nest the ticket under.
    category_id = cfg("server", "ticket_category_id", default=0)
    category = guild.get_channel(category_id) if category_id else None

    channel = await guild.create_text_channel(
        name=f"order-{random_code()}",
        overwrites=overwrites,
        category=category if isinstance(category, discord.CategoryChannel) else None,
    )

    await channel.send(
        content=user.mention,
        embed=ticket_info_embed(state, user),
        view=TicketView(),  # persistent buttons (Close / Request Support)
    )
    return channel


# -----------------------------------------------------------------------------
# 9) Ticket action buttons (persistent — they keep working after a restart)
# -----------------------------------------------------------------------------
class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent

        close = discord.ui.Button(
            label=cfg("text", "ticket", "close_button", "label", default="Close Ticket"),
            emoji=emoji_or_none(cfg("text", "ticket", "close_button", "emoji")),
            style=discord.ButtonStyle.danger,
            custom_id="ticket_close",   # fixed id so it survives restarts
        )
        support = discord.ui.Button(
            label=cfg("text", "ticket", "support_button", "label", default="Request Support"),
            emoji=emoji_or_none(cfg("text", "ticket", "support_button", "emoji")),
            style=discord.ButtonStyle.secondary,
            custom_id="ticket_support",
        )
        close.callback = self.on_close
        support.callback = self.on_support
        self.add_item(close)
        self.add_item(support)

    async def on_support(self, interaction: discord.Interaction):
        staff_role_id = cfg("server", "staff_role_id", default=0)
        staff_role = interaction.guild.get_role(staff_role_id) if staff_role_id else None
        staff_mention = staff_role.mention if staff_role else "@staff"
        msg = cfg("text", "ticket", "support_message", default="{staff} support requested.")
        await interaction.response.send_message(msg.format(staff=staff_mention))

    async def on_close(self, interaction: discord.Interaction):
        # Only staff (or anyone who can manage channels) may close a ticket.
        staff_role_id = cfg("server", "staff_role_id", default=0)
        has_staff_role = any(r.id == staff_role_id for r in getattr(interaction.user, "roles", []))
        can_manage = interaction.user.guild_permissions.manage_channels
        if not (has_staff_role or can_manage):
            await interaction.response.send_message(
                cfg("text", "errors", "no_permission"), ephemeral=True
            )
            return

        await interaction.response.send_message(
            cfg("text", "ticket", "closing_message", default="Closing…")
        )
        await asyncio.sleep(5)
        await interaction.channel.delete()


# -----------------------------------------------------------------------------
# 10) The panel view (the public message with the Start button)
#     Persistent so the button still works after the bot restarts.
# -----------------------------------------------------------------------------
class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

        # Optional link button (just opens a URL; no code runs).
        link_url = cfg("text", "panel", "link_button", "url", default="")
        if link_url:
            self.add_item(discord.ui.Button(
                label=cfg("text", "panel", "link_button", "label", default="Learn More"),
                emoji=emoji_or_none(cfg("text", "panel", "link_button", "emoji")),
                url=link_url, style=discord.ButtonStyle.link,
            ))

        start = discord.ui.Button(
            label=cfg("text", "panel", "start_button", "label", default="Start an Order"),
            emoji=emoji_or_none(cfg("text", "panel", "start_button", "emoji")),
            style=discord.ButtonStyle.primary,
            custom_id="panel_start",
        )
        start.callback = self.on_start
        self.add_item(start)

    async def on_start(self, interaction: discord.Interaction):
        # Begin a fresh order; first step = choose a company.
        state = OrderState()
        await interaction.response.send_message(
            embed=step_embed(cfg("text", "steps", "choose_company")),
            view=CompanyView(state), ephemeral=True,
        )


# =============================================================================
# 11) The bot itself + the /setup command
# =============================================================================
class OrderBot(commands.Bot):
    def __init__(self):
        # No privileged intents needed — this keeps setup simple.
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        # Register persistent views so buttons survive restarts.
        self.add_view(PanelView())
        self.add_view(TicketView())

        # Sync slash commands. With a guild_id set, this is instant.
        guild_id = cfg("server", "guild_id", default=0)
        if guild_id:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()


bot = OrderBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id}). Bot is ready.")


@bot.tree.command(name="setup", description="Post the order panel in the tickets channel.")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction):
    """Posts the panel. Admins only."""
    channel_id = cfg("server", "tickets_channel_id", default=0)
    target = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel

    await target.send(embed=panel_embed(), view=PanelView())
    await interaction.response.send_message(
        f"✅ Panel posted in {target.mention}.", ephemeral=True
    )


@setup.error
async def setup_error(interaction: discord.Interaction, error):
    # Friendly message if a non-admin tries to run /setup.
    await interaction.response.send_message(
        cfg("text", "errors", "no_permission"), ephemeral=True
    )


# -----------------------------------------------------------------------------
# 12) Start it up
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    bot.run(TOKEN)
