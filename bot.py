import logging
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("jellyseerr-bot")
CONFIG_DB = Path(
    os.environ.get(
        "BOT_CONFIG_PATH", str(Path(__file__).with_name("bot_config.sqlite3"))
    )
)
DISCORD_TOKEN = "PASTE_YOUR_DISCORD_BOT_TOKEN_HERE"


class JellyseerrAPIError(Exception):
    """Raised when Jellyseerr cannot complete a request."""


class ConfigStore:
    def __init__(self, database_path: Path = CONFIG_DB) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id INTEGER PRIMARY KEY,
                    base_url TEXT NOT NULL,
                    api_key TEXT NOT NULL
                )
                """
            )
        self.database_path.chmod(0o600)

    def save(self, guild_id: int, base_url: str, api_key: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO guild_config (guild_id, base_url, api_key)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    base_url = excluded.base_url,
                    api_key = excluded.api_key
                """,
                (guild_id, base_url, api_key),
            )

    def get(self, guild_id: int) -> tuple[str, str] | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT base_url, api_key FROM guild_config WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return (row[0], row[1]) if row else None

def validate_jellyseerr_url(base_url: str) -> str:
    normalized_url = base_url.strip().rstrip("/")
    parsed_url = urlsplit(normalized_url)
    if (
        parsed_url.scheme not in {"http", "https"}
        or not parsed_url.netloc
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ValueError(
            "Enter a Jellyseerr base URL beginning with http:// or https://, "
            "without a query string or fragment."
        )
    return normalized_url


def latest_pending_request(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list):
        requests = payload
    elif isinstance(payload, dict):
        requests = payload.get("results", payload.get("requests"))
    else:
        requests = None

    if not isinstance(requests, list):
        raise JellyseerrAPIError("Jellyseerr returned an unexpected request list.")

    for request in requests:
        if not isinstance(request, dict):
            continue
        status = request.get("status")
        if status is None or str(status).lower() in {"1", "pending"}:
            return request
    return None


def request_title(request: dict[str, Any]) -> str:
    media = request.get("media")
    if isinstance(media, dict):
        title = media.get("title") or media.get("name")
        if title:
            return str(title)
    return f"Request #{request.get('id', 'unknown')}"


class JellyseerrClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = validate_jellyseerr_url(base_url)
        self.headers = {"X-Api-Key": api_key}
        self.timeout = aiohttp.ClientTimeout(total=20)

    async def test_connection(self) -> None:
        async with aiohttp.ClientSession(
            headers=self.headers, timeout=self.timeout
        ) as session:
            async with session.get(
                f"{self.base_url}/api/v1/request",
                params={"take": 1, "skip": 0, "filter": "pending"},
            ) as response:
                if response.status >= 400:
                    raise JellyseerrAPIError(
                        f"Jellyseerr returned HTTP {response.status}. "
                        "Check the URL and API key."
                    )
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError) as error:
                    raise JellyseerrAPIError(
                        "Jellyseerr returned an invalid response."
                    ) from error
                if not isinstance(payload, (dict, list)):
                    raise JellyseerrAPIError(
                        "Jellyseerr returned an unexpected response."
                    )
                latest_pending_request(payload)

    async def accept_latest_pending(self) -> dict[str, Any] | None:
        async with aiohttp.ClientSession(
            headers=self.headers, timeout=self.timeout
        ) as session:
            async with session.get(
                f"{self.base_url}/api/v1/request",
                params={
                    "take": 20,
                    "skip": 0,
                    "filter": "pending",
                    "sort": "added",
                    "sortDirection": "desc",
                },
            ) as response:
                if response.status >= 400:
                    raise JellyseerrAPIError(
                        f"Jellyseerr could not list requests (HTTP {response.status})."
                    )
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError) as error:
                    raise JellyseerrAPIError(
                        "Jellyseerr returned an invalid response while listing requests."
                    ) from error

            request = latest_pending_request(payload)
            if request is None:
                return None

            request_id = request.get("id")
            if request_id is None:
                raise JellyseerrAPIError(
                    "The latest pending request did not include a request ID."
                )

            async with session.post(
                f"{self.base_url}/api/v1/request/{request_id}/approve"
            ) as response:
                if response.status >= 400:
                    raise JellyseerrAPIError(
                        f"Jellyseerr could not accept request #{request_id} "
                        f"(HTTP {response.status})."
                    )
            return request


class JellyseerrBot(discord.Client):
    def __init__(self, config_store: ConfigStore) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config_store = config_store

    async def setup_hook(self) -> None:
        await self.tree.sync()
        logger.info("Discord application commands synced.")


def has_manage_guild(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and (
        interaction.user.guild_permissions.manage_guild
    )


@app_commands.command(name="ping", description="Check the bot's response latency.")
async def ping(interaction: discord.Interaction) -> None:
    latency_ms = round(interaction.client.latency * 1000)
    await interaction.response.send_message(
        f"🏓 Pong!\nPing: {latency_ms}ms"
    )


@app_commands.command(
    name="setup",
    description="Connect this Discord server to Jellyseerr.",
)
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(
    url="Your Jellyseerr base URL, such as https://requests.example.com",
    api_key="An API key created in Jellyseerr settings",
)
async def setup(
    interaction: discord.Interaction,
    url: app_commands.Range[str, 1, 2000],
    api_key: app_commands.Range[str, 1, 255],
) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Run `/setup` in the Discord server you want to connect."
        )
        return
    if not has_manage_guild(interaction):
        await interaction.response.send_message(
            "You need the Manage Server permission to configure Jellyseerr."
        )
        return

    await interaction.response.defer(thinking=True)
    bot = interaction.client
    if not isinstance(bot, JellyseerrBot):
        logger.error("The setup command was invoked by an unexpected Discord client.")
        await interaction.followup.send(
            "The bot could not process this command. Please check the bot logs."
        )
        return

    try:
        normalized_url = validate_jellyseerr_url(url)
        if not api_key.strip():
            raise ValueError("The Jellyseerr API key cannot be empty.")
        client = JellyseerrClient(normalized_url, api_key.strip())
        await client.test_connection()
    except ValueError as error:
        await interaction.followup.send(str(error))
        return
    except (JellyseerrAPIError, aiohttp.ClientError, TimeoutError):
        logger.exception("Failed to verify Jellyseerr setup.")
        await interaction.followup.send(
            "Could not connect to Jellyseerr. Check the URL and API key, then try "
            "again. Details are recorded in the bot logs."
        )
        return

    try:
        bot.config_store.save(interaction.guild_id, normalized_url, api_key.strip())
    except sqlite3.Error:
        logger.exception(
            "Failed to save Jellyseerr setup to %s.",
            bot.config_store.database_path,
        )
        await interaction.followup.send(
            "The Jellyseerr connection worked, but the configuration could not be "
            "saved to the bot's local database. Check that the bot can write to "
            "the database file and its folder, then check the bot logs for details."
        )
        return

    await interaction.followup.send(
        "Jellyseerr is connected for this server. You can now use `/accept`."
    )


@app_commands.command(
    name="accept",
    description="Accept the latest pending Jellyseerr request.",
)
@app_commands.default_permissions(manage_guild=True)
async def accept(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Run `/accept` in the Discord server connected to Jellyseerr."
        )
        return
    if not has_manage_guild(interaction):
        await interaction.response.send_message(
            "You need the Manage Server permission to accept requests."
        )
        return

    await interaction.response.defer(thinking=True)
    bot = interaction.client
    if not isinstance(bot, JellyseerrBot):
        logger.error("The accept command was invoked by an unexpected Discord client.")
        await interaction.followup.send(
            "The bot could not process this command. Please check the bot logs."
        )
        return

    try:
        config = bot.config_store.get(interaction.guild_id)
    except sqlite3.Error:
        logger.exception("Failed to load Jellyseerr setup.")
        await interaction.followup.send(
            "The server configuration could not be loaded. Please check the bot "
            "logs."
        )
        return
    if config is None:
        await interaction.followup.send(
            "Jellyseerr is not connected for this server. Run `/setup` first."
        )
        return

    try:
        request = await JellyseerrClient(*config).accept_latest_pending()
    except (JellyseerrAPIError, aiohttp.ClientError, TimeoutError):
        logger.exception("Failed to accept the latest Jellyseerr request.")
        await interaction.followup.send(
            "Jellyseerr could not process the request. Please check the bot logs."
        )
        return

    if request is None:
        await interaction.followup.send(
            "There are no pending Jellyseerr requests to accept."
        )
        return

    requested_by = request.get("requestedBy")
    requester = (
        requested_by.get("displayName") or requested_by.get("username")
        if isinstance(requested_by, dict)
        else None
    )
    details = f" (requested by {requester})" if requester else ""
    await interaction.followup.send(
        f"Accepted **{request_title(request)}**{details}."
    )


def main() -> None:
    if DISCORD_TOKEN == "PASTE_YOUR_DISCORD_BOT_TOKEN_HERE":
        raise RuntimeError("Replace DISCORD_TOKEN with your bot token before starting.")

    bot = JellyseerrBot(ConfigStore())
    bot.tree.add_command(ping)
    bot.tree.add_command(setup)
    bot.tree.add_command(accept)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
