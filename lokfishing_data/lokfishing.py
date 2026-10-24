import asyncio
import io
import json
import logging
import math
import os
import random
import re
import time
from pathlib import Path

import discord
from redbot.core import Config, checks, commands

from .dashboard import FishingDashboard


log = logging.getLogger("red.lokfishing")

# This is the path inside the Redbot container.
# Docker maps the host cogs folder to /home/cogs.
AVATAR_DIR = Path("/home/cogs/lokfishing/avatars")

# Announce catches when the fish's overall chance per cast is at or below this.
ULTRARARE_THRESHOLD_PERCENT = 5.0

RARITIES = {"common", "rare"}
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
MAX_MENU_LOCATIONS = 25


def fish_slug(name: str) -> str:
    """Convert a fish name to a lowercase, hyphenated image filename."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "fish"


def normalize_location(value: str) -> str:
    """Accept names such as 'South Pole', 'south-pole', or 'south_pole'."""
    return re.sub(r"[\s-]+", "_", value.strip().lower())


def is_nothing(entry: dict) -> bool:
    """Return whether a table entry represents a no-catch outcome."""
    return (
        entry.get("type") == "nothing"
        or str(entry.get("name", "")).strip().casefold() == "nothing"
    )


def load_default_locations() -> dict:
    """Load and validate the default tables from the adjacent JSON file."""
    table_path = Path(__file__).with_name("loot_tables.json")

    try:
        with table_path.open("r", encoding="utf-8") as table_file:
            locations = json.load(table_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not load fishing tables from {table_path}: {exc}"
        ) from exc

    if not isinstance(locations, dict) or not locations:
        raise RuntimeError(
            f"{table_path} must contain a non-empty JSON object of locations."
        )

    for key, location in locations.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key):
            raise RuntimeError(f"Invalid location key in {table_path}: {key!r}")

        if not isinstance(location, dict):
            raise RuntimeError(f"Location {key!r} must be a JSON object.")

        label = location.get("label")
        if not isinstance(label, str) or not label.strip():
            raise RuntimeError(f"Location {key!r} needs a non-empty label.")

        try:
            rare_chance = float(location["rare_chance"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Location {key!r} needs a valid rare_chance."
            ) from exc

        if not math.isfinite(rare_chance) or not 0 <= rare_chance <= 100:
            raise RuntimeError(
                f"Location {key!r} rare_chance must be between 0 and 100."
            )

        for rarity in RARITIES:
            table = location.get(rarity)
            if not isinstance(table, list) or not table:
                raise RuntimeError(
                    f"Location {key!r} needs a non-empty {rarity} table."
                )

            for entry in table:
                if not isinstance(entry, dict):
                    raise RuntimeError(
                        f"Location {key!r} has an invalid {rarity} entry."
                    )

                name = entry.get("name")
                if not isinstance(name, str) or not name.strip():
                    raise RuntimeError(
                        f"Location {key!r} has an entry without a name."
                    )

                try:
                    weight = float(entry.get("weight", 0))
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        f"Location {key!r} has an invalid weight."
                    ) from exc

                if not math.isfinite(weight) or weight <= 0:
                    raise RuntimeError(
                        f"Location {key!r} weights must be positive numbers."
                    )

    return locations


# This is loaded once when Python imports the cog, not for every catch.
DEFAULT_LOCATIONS = load_default_locations()


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
    def __init__(self, cog, locations: dict):
        super().__init__(timeout=900)
        self.message = None

        # Discord views support at most 25 components.
        for location_key, location in list(locations.items())[:MAX_MENU_LOCATIONS]:
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
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                pass


class LokFishing(commands.Cog):
    """Button-driven fishing game with a local dashboard."""

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

        # Cooldowns reset on bot restart. Inventories persist in Redbot Config.
        self._last_catch = {}
        self._state_lock = asyncio.Lock()
        self._dashboard = None

    async def cog_load(self):
        """Start the optional dashboard if its password is configured."""
        password = os.getenv("LOKFISHING_DASHBOARD_PASSWORD")

        if not password:
            log.info(
                "LokFishing dashboard disabled: "
                "LOKFISHING_DASHBOARD_PASSWORD is not set."
            )
            return

        if len(password) < 24:
            log.error(
                "LokFishing dashboard disabled: password must be at least "
                "24 characters long."
            )
            return

        try:
            port = int(os.getenv("LOKFISHING_DASHBOARD_PORT", "8788"))
            if not 1 <= port <= 65535:
                raise ValueError("Dashboard port must be between 1 and 65535.")

            self._dashboard = FishingDashboard(self, password, port)
            await self._dashboard.start()
            log.info("LokFishing dashboard started on port %s.", port)

        except Exception:
            self._dashboard = None
            log.exception("Could not start the LokFishing dashboard.")

    async def cog_unload(self):
        """Stop the dashboard when the cog unloads."""
        if self._dashboard is not None:
            await self._dashboard.close()
            self._dashboard = None

    @commands.group(name="lokfish", invoke_without_command=True)
    async def lokfish(self, ctx):
        """Open the fishing location menu."""
        locations = await self.config.locations()

        if not locations:
            await ctx.send("No fishing locations are configured.")
            return

        view = FishingMenu(self, locations)
        message = await ctx.send(
            "**Legend of Korra Fishing**\n"
            "Choose a location to cast your line:",
            view=view,
        )
        view.message = message

        if len(locations) > MAX_MENU_LOCATIONS:
            await ctx.send(
                f"Only the first {MAX_MENU_LOCATIONS} locations can appear "
                "in this menu."
            )

    @staticmethod
    def _validated_table(location: dict, rarity: str):
        """Return a table and its validated positive weights."""
        table = location.get(rarity)

        if not isinstance(table, list) or not table:
            raise ValueError(f"The {rarity} table is empty or invalid.")

        weights = []
        for entry in table:
            if not isinstance(entry, dict):
                raise ValueError(f"The {rarity} table contains an invalid entry.")

            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"The {rarity} table has an entry without a name.")

            weight = float(entry.get("weight", 0))
            if not math.isfinite(weight) or weight <= 0:
                raise ValueError(f"The {rarity} table has an invalid weight.")

            weights.append(weight)

        return table, weights

    def _fish_chance_percent(
        self,
        location: dict,
        rare_chance: float,
        fish_name: str,
    ) -> float:
        """Calculate a fish species' overall chance across both tables."""
        total_chance = 0.0
        branch_chances = {
            "common": 100.0 - rare_chance,
            "rare": rare_chance,
        }

        for rarity, branch_chance in branch_chances.items():
            if branch_chance <= 0:
                continue

            table, weights = self._validated_table(location, rarity)
            total_weight = sum(weights)

            for entry, weight in zip(table, weights):
                if (
                    not is_nothing(entry)
                    and entry["name"].strip().casefold()
                    == fish_name.strip().casefold()
                ):
                    total_chance += branch_chance * weight / total_weight

        return min(total_chance, 100.0)

    async def handle_catch(
        self,
        interaction: discord.Interaction,
        location_key: str,
    ):
        if interaction.user.bot:
            await interaction.response.send_message(
                "Bots can't fish.",
                ephemeral=True,
            )
            return

        # Acknowledge promptly while Config and local images are processed.
        await interaction.response.defer(ephemeral=True)

        user_id = interaction.user.id
        now = time.monotonic()

        cooldown_message = None
        config_error = False
        missing_location = False
        no_catch = False
        rarity = None
        fish_name = None
        amount = 0
        fish_chance_percent = None
        location = None

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if not location:
                missing_location = True
            else:
                cooldown_seconds = await self.config.cooldown_seconds()
                last_catch = self._last_catch.get(user_id)

                if last_catch is not None:
                    remaining = cooldown_seconds - (now - last_catch)
                    if remaining > 0:
                        wait_seconds = math.ceil(remaining)
                        cooldown_message = (
                            f"Your line is still out. Try again in "
                            f"**{wait_seconds}** second"
                            f"{'s' if wait_seconds != 1 else ''}."
                        )

                if cooldown_message is None:
                    try:
                        rare_chance = float(location["rare_chance"])
                        if (
                            not math.isfinite(rare_chance)
                            or not 0 <= rare_chance <= 100
                        ):
                            raise ValueError(
                                "Rare chance must be between 0 and 100."
                            )

                        rarity = random.choices(
                            ["rare", "common"],
                            weights=[rare_chance, 100.0 - rare_chance],
                            k=1,
                        )[0]

                        fish_table, weights = self._validated_table(
                            location,
                            rarity,
                        )
                        outcome = random.choices(
                            fish_table,
                            weights=weights,
                            k=1,
                        )[0]

                        no_catch = is_nothing(outcome)

                        if no_catch:
                            # A miss still counts as a cast for cooldown.
                            self._last_catch[user_id] = now
                        else:
                            fish_name = outcome["name"]
                            fish_chance_percent = self._fish_chance_percent(
                                location,
                                rare_chance,
                                fish_name,
                            )
                            amount = random.randint(1, 3)

                            user_config = self.config.user(interaction.user)
                            inventory = await user_config.inventory()
                            inventory[fish_name] = (
                                int(inventory.get(fish_name, 0)) + amount
                            )
                            await user_config.inventory.set(inventory)

                            self._last_catch[user_id] = now

                    except (KeyError, TypeError, ValueError):
                        config_error = True
                        log.exception(
                            "Invalid fishing table configuration for %s",
                            location_key,
                        )

        if missing_location:
            await interaction.followup.send(
                "That fishing location is not configured.",
                ephemeral=True,
            )
            return

        if cooldown_message:
            await interaction.followup.send(
                cooldown_message,
                ephemeral=True,
            )
            return

        if config_error:
            await interaction.followup.send(
                "This location's fishing table is misconfigured. "
                "Ask an admin to check `!lokfish settings`.",
                ephemeral=True,
            )
            return

        if no_catch:
            embed = discord.Embed(
                title="No catch this time",
                description=(
                    f"You cast your line at **{location['label']}**, "
                    "but caught nothing."
                ),
                color=discord.Color.dark_grey(),
            )
            embed.add_field(name="Rarity roll", value=rarity.title())
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"You caught {fish_name}!",
            description=(
                f"**{amount} × {fish_name}** caught at "
                f"**{location['label']}**."
            ),
            color=(
                discord.Color.gold()
                if rarity == "rare"
                else discord.Color.blue()
            ),
        )
        embed.add_field(name="Rarity", value=rarity.title())
        embed.add_field(name="Added to your inventory", value=str(amount))

        avatar_data = await asyncio.to_thread(self._make_avatar, fish_name)

        if avatar_data is not None:
            image_bytes, filename = avatar_data
            embed.set_thumbnail(url=f"attachment://{filename}")
            file = discord.File(io.BytesIO(image_bytes), filename=filename)
            await interaction.followup.send(
                embed=embed,
                file=file,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=embed,
                ephemeral=True,
            )

        # Public ultra-rare announcement. Discord schedules deletion after one
        # hour; a bot restart before then may prevent the message being deleted.
        if (
            fish_chance_percent is not None
            and fish_chance_percent <= ULTRARARE_THRESHOLD_PERCENT
            and interaction.guild is not None
            and interaction.channel is not None
        ):
            try:
                await interaction.channel.send(
                    f"✨ {interaction.user.mention} caught an ultra-rare "
                    f"**{fish_name}** at **{location['label']}**! "
                    f"(about **{fish_chance_percent:.2f}%** chance per cast)",
                    delete_after=3600,
                    allowed_mentions=discord.AllowedMentions(
                        users=True,
                        roles=False,
                        everyone=False,
                    ),
                )
            except discord.HTTPException:
                log.exception("Could not send ultra-rare catch announcement.")

    @staticmethod
    def _make_avatar(fish_name: str):
        """Load a local avatar, resize it to 300×300, and return PNG bytes."""
        slug = fish_slug(fish_name)

        image_path = next(
            (
                AVATAR_DIR / f"{slug}{extension}"
                for extension in IMAGE_EXTENSIONS
                if (AVATAR_DIR / f"{slug}{extension}").is_file()
            ),
            None,
        )

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
                return buffer.getvalue(), "fish-avatar.png"

        except Exception:
            log.exception("Could not process fish avatar %s", image_path)
            return None

    @lokfish.command(name="profile", aliases=["inventory"])
    async def profile(self, ctx, member: discord.Member = None):
        """Show your inventory or another member's inventory."""
        member = member or ctx.author
        inventory = await self.config.user(member).inventory()

        embed = discord.Embed(
            title=f"{member.display_name}'s Fish Inventory",
            color=discord.Color.blue(),
        )

        if not inventory:
            embed.description = (
                "No fish caught yet. Use `!lokfish` to start fishing."
            )
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
        """Show current fishing rates and weighted tables."""
        locations = await self.config.locations()
        cooldown = await self.config.cooldown_seconds()

        embed = discord.Embed(
            title="Fishing Settings",
            description=(
                f"Per-player cooldown: **{cooldown} seconds**\n"
                "The bot rolls common or rare first, then selects an entry "
                "from that table using relative weights. `Nothing` is not "
                "added to inventory."
            ),
            color=discord.Color.blue(),
        )

        for location in locations.values():
            common = ", ".join(
                f"{entry['name']} (weight {entry['weight']})"
                for entry in location.get("common", [])[:10]
            ) or "—"
            rare = ", ".join(
                f"{entry['name']} (weight {entry['weight']})"
                for entry in location.get("rare", [])[:10]
            ) or "—"

            embed.add_field(
                name=(
                    f"{location['label']} — "
                    f"rare chance {location['rare_chance']}%"
                ),
                value=f"**Common:** {common}\n**Rare:** {rare}",
                inline=False,
            )

        await ctx.send(embed=embed)

    @lokfish.command(name="setrare")
    @checks.admin_or_permissions(manage_guild=True)
    async def setrare(self, ctx, location_name: str, percent: float):
        """Set rare chance. Example: !lokfish setrare south_pole 20"""
        location_key = normalize_location(location_name)

        if not math.isfinite(percent) or not 0 <= percent <= 100:
            await ctx.send("The rare chance must be between 0 and 100.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                await ctx.send(
                    "Unknown location. Use the dashboard to manage locations."
                )
                return

            location["rare_chance"] = float(percent)
            await self.config.locations.set(locations)

        await ctx.send(
            f"Rare-catch chance at **{location['label']}** "
            f"is now **{percent:g}%**."
        )

    @lokfish.command(name="setcooldown")
    @checks.admin_or_permissions(manage_guild=True)
    async def setcooldown(self, ctx, seconds: int):
        """Set the per-player cooldown in seconds; 0 disables it."""
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
        """Add a fish. Example: !lokfish addfish south_pole rare 2 Spirit Koi"""
        location_key = normalize_location(location_name)
        rarity = rarity.lower()

        if rarity not in RARITIES:
            await ctx.send("Rarity must be `common` or `rare`.")
            return

        if not math.isfinite(weight) or weight <= 0:
            await ctx.send("Weight must be a positive number.")
            return

        fish_name = fish_name.strip()
        if not fish_name or len(fish_name) > 80:
            await ctx.send("Fish names must be between 1 and 80 characters.")
            return

        if fish_name.casefold() == "nothing":
            await ctx.send("Use `addnothing` to add a no-catch outcome.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                await ctx.send(
                    "Unknown location. Use the dashboard to manage locations."
                )
                return

            table = location[rarity]
            if any(
                entry["name"].casefold() == fish_name.casefold()
                for entry in table
            ):
                await ctx.send("That fish is already in this rarity table.")
                return

            if len(table) >= 100:
                await ctx.send(
                    "That rarity table already has the maximum of 100 entries."
                )
                return

            table.append({"name": fish_name, "weight": float(weight)})
            await self.config.locations.set(locations)

        await ctx.send(
            f"Added **{fish_name}** to the **{rarity}** table at "
            f"**{location['label']}** with weight **{weight:g}**."
        )

    @lokfish.command(name="addnothing")
    @checks.admin_or_permissions(manage_guild=True)
    async def addnothing(self, ctx, location_name: str, weight: float):
        """Add a no-catch outcome to a location's common table."""
        location_key = normalize_location(location_name)

        if not math.isfinite(weight) or weight <= 0:
            await ctx.send("Weight must be a positive number.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                await ctx.send(
                    "Unknown location. Use the dashboard to manage locations."
                )
                return

            table = location["common"]
            if any(is_nothing(entry) for entry in table):
                await ctx.send("That common table already has a Nothing outcome.")
                return

            if len(table) >= 100:
                await ctx.send(
                    "That common table already has the maximum of 100 entries."
                )
                return

            table.append(
                {"name": "Nothing", "weight": float(weight), "type": "nothing"}
            )
            await self.config.locations.set(locations)

        await ctx.send(
            f"Added **Nothing** to the common table at "
            f"**{location['label']}** with weight **{weight:g}**."
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
        """Remove a fish or Nothing entry from a drop table."""
        location_key = normalize_location(location_name)
        rarity = rarity.lower()

        if rarity not in RARITIES:
            await ctx.send("Rarity must be `common` or `rare`.")
            return

        async with self._state_lock:
            locations = await self.config.locations()
            location = locations.get(location_key)

            if location is None:
                await ctx.send(
                    "Unknown location. Use the dashboard to manage locations."
                )
                return

            table = location[rarity]
            match = next(
                (
                    entry
                    for entry in table
                    if entry["name"].strip().casefold()
                    == fish_name.strip().casefold()
                ),
                None,
            )

            if match is None:
                await ctx.send("That entry is not in the specified table.")
                return

            if len(table) <= 1:
                await ctx.send(
                    "Each rarity table must keep at least one entry. "
                    "Add a replacement first."
                )
                return

            table.remove(match)
            await self.config.locations.set(locations)

        await ctx.send(
            f"Removed **{match['name']}** from the **{rarity}** table at "
            f"**{location['label']}**."
        )