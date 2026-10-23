import asyncio
import io
import logging
import math
import random
import re
import time
from pathlib import Path

import discord
from redbot.core import Config, checks, commands


log = logging.getLogger("red.lokfishing")

# This path must exist inside the container running Redbot.
AVATAR_DIR = Path("/home/cogs/lokfishing/avatars")

# Starter species and weights are examples you can change with the admin commands.
# Fish weights are relative weights within their rarity table, not percentages.
DEFAULT_LOCATIONS = {
    "south_pole": {
        "label": "South Pole",
        "rare_chance": 5.0,
        "common": [
            {"name": "Polar Icefish", "weight": 5.0},
            {"name": "Snowfin Trout", "weight": 3.0},
        ],
        "rare": [
            {"name": "Moonlit Spirit Koi", "weight": 2.0},
            {"name": "Glacier Crown Salmon", "weight": 1.0},
        ],
    },
    "republic_city": {
        "label": "Republic City",
        "rare_chance": 8.0,
        "common": [
            {"name": "Canal Carp", "weight": 5.0},
            {"name": "Harbor Bluegill", "weight": 3.0},
        ],
        "rare": [
            {"name": "Lanternfin Koi", "weight": 2.0},
            {"name": "Platinum Eel", "weight": 1.0},
        ],
    },
    "pro_bending_arena": {
        "label": "Pro Bending Arena",
        "rare_chance": 12.0,
        "common": [
            {"name": "Arena Minnow", "weight": 5.0},
            {"name": "Ringwater Carp", "weight": 3.0},
        ],
        "rare": [
            {"name": "Champion's Goldfin", "weight": 2.0},
            {"name": "Lightningstripe Eel", "weight": 1.0},
        ],
    },
}

RARITIES = {"common", "rare"}
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def fish_slug(name: str) -> str:
    """Convert a fish name to the expected lowercase, hyphenated filename."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "fish"


def normalize_location(value: str) -> str:
    """Accept names like 'South Pole', 'south-pole', or 'south_pole'."""
    return re.sub(r"[\s-]+", "_", value.strip().lower())


class LocationButton(discord.ui.Button):
    def __init__(self, cog, location_key: str, label: str):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"lokfishing:{location_key}",
        )
        self.cog = cog
        self.location_key = location_key

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_catch(interaction, self.location_key)


class FishingMenu(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=900)
        self.cog = cog
        self.message = None

        locations = cog.default_locations
        for location_key, location in locations.items():
            self.add_item(
                LocationButton(
                    cog=cog,
                    location_key=location_key,
                    label=location["label"],
                )
            )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True

        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


class LokFishing(commands.Cog):
    """A button-driven fishing mini game."""

    default_locations = DEFAULT_LOCATIONS

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(
            self,
            identifier=0x4C4F4B46495348,
            force_registration=True,
        )
        self.config.register_global(
            locations=DEFAULT_LOCATIONS,
            cooldown_seconds=30,
        )
        self.config.register_user(inventory={})

        # In-memory cooldowns reset if the bot restarts. Inventories do not.
        self._last_catch = {}
        self._state_lock = asyncio.Lock()

    @commands.group(name="lokfish", invoke_without_command=True)
    async def lokfish(self, ctx):
        """Open the fishing location menu."""
        view = FishingMenu(self)
        message = await ctx.send(
            "**Legend of Korra Fishing**\nChoose a location to cast your line:",
            view=view,
        )
        view.message = message

    async def handle_catch(self, interaction: discord.Interaction, location_key: str):
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bots can't fish.",
                ephemeral=True,
            )
            return

        locations = await self.config.locations()
        location = locations.get(location_key)

        if not location:
            await interaction.response.send_message(
                "That fishing location is not configured.",
                ephemeral=True,
            )
            return

        cooldown_seconds = await self.config.cooldown_seconds()
        user_id = interaction.user.id
        now = time.monotonic()

        async with self._state_lock:
            last_catch = self._last_catch.get(user_id)
            if last_catch is not None:
                remaining = cooldown_seconds - (now - last_catch)
                if remaining > 0:
                    wait_seconds = math.ceil(remaining)
                    await interaction.response.send_message(
                        f"Your line is still out. Try again in **{wait_seconds}** "
                        f"second{'s' if wait_seconds != 1 else ''}.",
                        ephemeral=True,
                    )
                    return

            rare_chance = float(location["rare_chance"])
            rarity = random.choices(
                ["rare", "common"],
                weights=[rare_chance, 100.0 - rare_chance],
                k=1,
            )[0]

            fish_table = location[rarity]
            if not fish_table:
                await interaction.response.send_message(
                    f"The {rarity} fish table at this location is empty. "
                    "Ask a server admin to add fish.",
                    ephemeral=True,
                )
                return

            fish = random.choices(
                fish_table,
                weights=[float(entry["weight"]) for entry in fish_table],
                k=1,
            )[0]
            fish_name = fish["name"]
            amount = random.randint(1, 3)

            inventory = await self.config.user(interaction.user).inventory()
            inventory[fish_name] = int(inventory.get(fish_name, 0)) + amount
            await self.config.user(interaction.user).inventory.set(inventory)

            self._last_catch[user_id] = now

        embed = discord.Embed(
            title=f"You caught {fish_name}!",
            description=(
                f"**{amount} × {fish_name}** caught at "
                f"**{location['label']}**."
            ),
            color=discord.Color.blue() if rarity == "common" else discord.Color.gold(),
        )
        embed.add_field(name="Rarity", value=rarity.title())
        embed.add_field(name="Added to your inventory", value=str(amount))

        response_kwargs = {
            "embed": embed,
            "ephemeral": True,
        }

        avatar = await asyncio.to_thread(self._make_avatar, fish_name)
        if avatar is not None:
            file, filename = avatar
            embed.set_thumbnail(url=f"attachment://{filename}")
            response_kwargs["file"] = file

        await interaction.response.send_message(**response_kwargs)

    @staticmethod
    def _make_avatar(fish_name: str):
        """Load a local fish avatar, resize it to 300x300, and return a Discord file."""
        slug = fish_slug(fish_name)

        image_path = None
        for extension in IMAGE_EXTENSIONS:
            candidate = AVATAR_DIR / f"{slug}{extension}"
            if candidate.is_file():
                image_path = candidate
                break

        if image_path is None:
            return None

        try:
            from PIL import Image

            with Image.open(image_path) as source:
                image = source.convert("RGBA")
                resampling = getattr(Image, "Resampling", Image)
                image = image.resize((300, 300), resampling.LANCZOS)

                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                buffer.seek(0)

            filename = "fish-avatar.png"
            return discord.File(buffer, filename=filename), filename
        except Exception:
            log.exception("Could not process fish avatar %s", image_path)
            return None

    @lokfish.command(name="profile", aliases=["inventory"])
    async def profile(self, ctx, member: discord.Member = None):
        """Show your fish inventory, or another member's inventory."""
        member = member or ctx.author
        inventory = await self.config.user(member).inventory()

        embed = discord.Embed(
            title=f"{member.display_name}'s Fish Inventory",
            color=discord.Color.blue(),
        )

        if not inventory:
            embed.description = "No fish caught yet. Use `[p]lokfish` to start fishing."
        else:
            entries = sorted(
                inventory.items(),
                key=lambda item: (-int(item[1]), item[0].lower()),
            )
            lines = [f"**{name}** — {amount}" for name, amount in entries]
            description = "\n".join(lines)

            if len(description) > 3900:
                description = description[:3850].rsplit("\n", 1)[0]
                description += "\n…inventory truncated."

            embed.description = description

        await ctx.send(embed=embed)

    @lokfish.command(name="settings")
    @checks.admin_or_permissions(manage_guild=True)
    async def settings(self, ctx):
        """Show fishing rates, cooldown, and fish table weights."""
        locations = await self.config.locations()
        cooldown = await self.config.cooldown_seconds()

        embed = discord.Embed(
            title="Fishing Settings",
            description=(
                f"Per-player cooldown: **{cooldown} seconds**\n"
                "Rare chance is rolled first; fish weights are relative weights "
                "within that rarity table."
            ),
            color=discord.Color.blue(),
        )

        for location_key, location in locations.items():
            common = ", ".join(
                f"{entry['name']} (weight {entry['weight']})"
                for entry in location["common"][:10]
            ) or "—"
            rare = ", ".join(
                f"{entry['name']} (weight {entry['weight']})"
                for entry in location["rare"][:10]
            ) or "—"

            embed.add_field(
                name=f"{location['label']} — rare chance {location['rare_chance']}%",
                value=f"**Common:** {common}\n**Rare:** {rare}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @lokfish.command(name="setrare")
    @checks.admin_or_permissions(manage_guild=True)
    async def setrare(self, ctx, location_name: str, percent: float):
        """Set a location's rare-catch chance. Example: [p]lokfish setrare south_pole 5"""
        location_key = normalize_location(location_name)

        if not 0 <= percent <= 100:
            await ctx.send("The rare chance must be between 0 and 100.")
            return

        locations = await self.config.locations()
        if location_key not in locations:
            await ctx.send(
                "Unknown location. Use `south_pole`, `republic_city`, "
                "or `pro_bending_arena`."
            )
            return

        locations[location_key]["rare_chance"] = float(percent)
        await self.config.locations.set(locations)
        await ctx.send(
            f"Rare-catch chance at **{locations[location_key]['label']}** "
            f"is now **{percent:g}%**."
        )

    @lokfish.command(name="setcooldown")
    @checks.admin_or_permissions(manage_guild=True)
    async def setcooldown(self, ctx, seconds: int):
        """Set the per-player fishing cooldown in seconds; 0 disables it."""
        if not 0 <= seconds <= 86400:
            await ctx.send("Cooldown must be between 0 and 86400 seconds.")
            return

        await self.config.cooldown_seconds.set(seconds)
        await ctx.send(f"Fishing cooldown set to **{seconds} seconds**.")

    @lokfish.command(name="addfish")
    @checks.admin_or_permissions(manage_guild=True)
    async def addfish(
        self,
        ctx,
        location_name: str,
        rarity: str,
        weight: float,
        *,
        fish_name: str,
    ):
        """Add a fish. Example: [p]lokfish addfish south_pole rare 2 Spirit Koi"""
        location_key = normalize_location(location_name)
        rarity = rarity.lower()

        if rarity not in RARITIES:
            await ctx.send("Rarity must be `common` or `rare`.")
            return

        if not math.isfinite(weight) or weight <= 0:
            await ctx.send("Fish weight must be a positive number.")
            return

        fish_name = fish_name.strip()
        if not fish_name or len(fish_name) > 80:
            await ctx.send("Fish names must be between 1 and 80 characters.")
            return

        locations = await self.config.locations()
        if location_key not in locations:
            await ctx.send(
                "Unknown location. Use `south_pole`, `republic_city`, "
                "or `pro_bending_arena`."
            )
            return

        table = locations[location_key][rarity]
        if any(entry["name"].casefold() == fish_name.casefold() for entry in table):
            await ctx.send("That fish is already in this rarity table.")
            return

        if len(table) >= 100:
            await ctx.send("That rarity table already has the maximum of 100 fish.")
            return

        table.append({"name": fish_name, "weight": float(weight)})
        await self.config.locations.set(locations)
        await ctx.send(
            f"Added **{fish_name}** to the **{rarity}** table at "
            f"**{locations[location_key]['label']}** with weight **{weight:g}**."
        )

    @lokfish.command(name="removefish")
    @checks.admin_or_permissions(manage_guild=True)
    async def removefish(
        self,
        ctx,
        location_name: str,
        rarity: str,
        *,
        fish_name: str,
    ):
        """Remove a fish. Example: [p]lokfish removefish south_pole rare Spirit Koi"""
        location_key = normalize_location(location_name)
        rarity = rarity.lower()

        if rarity not in RARITIES:
            await ctx.send("Rarity must be `common` or `rare`.")
            return

        locations = await self.config.locations()
        if location_key not in locations:
            await ctx.send(
                "Unknown location. Use `south_pole`, `republic_city`, "
                "or `pro_bending_arena`."
            )
            return

        table = locations[location_key][rarity]
        match = next(
            (
                entry
                for entry in table
                if entry["name"].casefold() == fish_name.strip().casefold()
            ),
            None,
        )

        if match is None:
            await ctx.send("That fish is not in the specified table.")
            return

        if len(table) == 1:
            await ctx.send(
                "Each rarity table must keep at least one fish. "
                "Add a replacement first."
            )
            return

        table.remove(match)
        await self.config.locations.set(locations)
        await ctx.send(
            f"Removed **{match['name']}** from the **{rarity}** table at "
            f"**{locations[location_key]['label']}**."
        )