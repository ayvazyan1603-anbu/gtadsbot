"""
Общий модуль конфигурации бота.

Все ID ролей/каналов/категорий хранятся в базе данных (таблица bot_config)
и настраиваются ОДНОЙ командой /config прямо в Discord — без правки кода.

Использование в других когах:

    from cogs.config import get_id, get_list

    role_id = await get_id(interaction.guild_id, "hr_role_id")
    excluded = await get_list(interaction.guild_id, "excluded_voice_channels")
"""

import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite

DB = "bot.db"

# ────────────────────────────────────────────────────────────
#  Реестр всех настраиваемых ID (ключ -> описание)
#  type: "role" | "channel" | "category"
#  is_list: True для настроек-списков (несколько ролей/каналов)
# ────────────────────────────────────────────────────────────
CONFIG_KEYS: dict[str, dict] = {
    # Общие роли
    "admin_role_id":        {"label": "Роль администратора",                       "type": "role",     "is_list": False},
    "hr_role_id":           {"label": "Роль HR",                                   "type": "role",     "is_list": False},
    "dep_owner_role_id":    {"label": "Роль Dep Owner",                            "type": "role",     "is_list": False},
    "recruit_role_id":      {"label": "Роль Recruit",                              "type": "role",     "is_list": False},
    "member_role_id":       {"label": "Роль при одобрении заявки в семью",                      "type": "role",     "is_list": False},
    "afk_role_id":          {"label": "Роль АФК",                                  "type": "role",     "is_list": False},
    "vacation_role_id":     {"label": "Роль 'В отпуске'",                          "type": "role",     "is_list": False},
    "vip_role_id":          {"label": "Роль VIP (магазин)",                        "type": "role",     "is_list": False},

    # Каналы / категории
    "afk_log_channel":      {"label": "Канал логов АФК",                           "type": "channel",  "is_list": False},
    "promo_log_channel":    {"label": "Канал логов повышений",                     "type": "channel",  "is_list": False},
    "vacation_log_channel": {"label": "Канал логов отпусков",                      "type": "channel",  "is_list": False},
    "voice_log_channel":    {"label": "Канал логов войса",                         "type": "channel",  "is_list": False},
    "app_review_channel":   {"label": "Канал заявок в семью",                      "type": "channel",  "is_list": False},
    "app_log_channel":      {"label": "Канал логов заявок в семью",                "type": "channel",  "is_list": False},
    "app_category_id":      {"label": "Категория заявок в семью",                  "type": "category", "is_list": False},
    "app_ping_mention":     {"label": "Пинг при новой заявке (роль или юзер)",          "type": "role",     "is_list": False},
    "app_panel_title":      {"label": "Заголовок панели заявок (текст)",                "type": "text",     "is_list": False},
    "app_panel_desc":       {"label": "Описание панели заявок (текст)",                 "type": "text",     "is_list": False},

    # Списки
    "allowed_roles":            {"label": "Роли, доступные для выдачи (HR-команды)", "type": "role",    "is_list": True},
    "excluded_voice_channels":  {"label": "Голосовые каналы вне трекера",            "type": "channel",  "is_list": True},
}


# ════════════════════════════════════════════════
#  ХЕЛПЕРЫ ХРАНИЛИЩА
# ════════════════════════════════════════════════

async def get_value(guild_id: int, key: str) -> str | None:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT value FROM bot_config WHERE guild_id=? AND key=?", (guild_id, key)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def get_id(guild_id: int, key: str, default: int = 0) -> int:
    """Возвращает сохранённый ID (роль/канал/категория) или default, если не настроено."""
    value = await get_value(guild_id, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


async def set_value(guild_id: int, key: str, value: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            """INSERT INTO bot_config (guild_id, key, value) VALUES (?,?,?)
               ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value""",
            (guild_id, key, str(value)),
        )
        await db.commit()


async def clear_value(guild_id: int, key: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM bot_config WHERE guild_id=? AND key=?", (guild_id, key))
        await db.commit()


async def get_all(guild_id: int) -> dict[str, str]:
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT key, value FROM bot_config WHERE guild_id=?", (guild_id,)
        ) as cur:
            rows = await cur.fetchall()
    return dict(rows)


# ── Списки (allowed_roles, excluded_voice_channels) ─────────

async def get_list(guild_id: int, key: str) -> list[int]:
    value = await get_value(guild_id, key)
    if not value:
        return []
    out = []
    for part in value.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


async def add_to_list(guild_id: int, key: str, item_id: int):
    items = await get_list(guild_id, key)
    if item_id not in items:
        items.append(item_id)
    await set_value(guild_id, key, ",".join(str(i) for i in items))


async def remove_from_list(guild_id: int, key: str, item_id: int) -> bool:
    items = await get_list(guild_id, key)
    if item_id not in items:
        return False
    items.remove(item_id)
    await set_value(guild_id, key, ",".join(str(i) for i in items))
    return True


# ════════════════════════════════════════════════
#  ФОРМАТИРОВАНИЕ ТЕКУЩЕГО ЗНАЧЕНИЯ
# ════════════════════════════════════════════════

async def _format_status(guild_id: int, key: str, info: dict) -> str:
    """Короткая строка текущего значения для конкретного ключа."""
    if info["is_list"]:
        ids = await get_list(guild_id, key)
        if not ids:
            return "пусто"
        fmt = "<@&{}>" if info["type"] == "role" else "<#{}>"
        return ", ".join(fmt.format(i) for i in ids)

    value = await get_value(guild_id, key)
    if not value:
        return "не настроено"
    if info["type"] == "role":
        return f"<@&{value}>"
    if info["type"] == "channel":
        return f"<#{value}>"
    if info["type"] == "text":
        return f"`{value[:60]}{'…' if len(value) > 60 else ''}`"
    return f"`{value}` (категория)"


# ════════════════════════════════════════════════
#  UI: ВТОРОЙ ШАГ — ВЫБОР ЗНАЧЕНИЯ ДЛЯ ВЫБРАННОГО КЛЮЧА
# ════════════════════════════════════════════════

class _RoleSetSelect(discord.ui.RoleSelect):
    def __init__(self, key: str, info: dict, add: bool):
        self.key = key
        self.info = info
        self.add = add
        action = "Добавить" if add else "Привязать"
        super().__init__(placeholder=f"{action} роль: {info['label']}", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        if self.info["is_list"]:
            if self.add:
                await add_to_list(interaction.guild_id, self.key, role.id)
                text = f"✅ {role.mention} добавлена в **{self.info['label']}**."
            else:
                ok = await remove_from_list(interaction.guild_id, self.key, role.id)
                text = (
                    f"✅ {role.mention} убрана из **{self.info['label']}**."
                    if ok else "⚠️ Этой роли не было в списке."
                )
        else:
            await set_value(interaction.guild_id, self.key, role.id)
            text = f"✅ **{self.info['label']}** → {role.mention}"
        await interaction.response.edit_message(content=text, view=None)


class _ChannelSetSelect(discord.ui.ChannelSelect):
    def __init__(self, key: str, info: dict, add: bool):
        self.key = key
        self.info = info
        self.add = add
        action = "Добавить" if add else "Привязать"
        channel_types = (
            [discord.ChannelType.category]
            if info["type"] == "category"
            else [discord.ChannelType.text, discord.ChannelType.voice, discord.ChannelType.news]
        )
        super().__init__(
            placeholder=f"{action} канал: {info['label']}",
            min_values=1, max_values=1,
            channel_types=channel_types,
        )

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        if self.info["is_list"]:
            if self.add:
                await add_to_list(interaction.guild_id, self.key, channel.id)
                text = f"✅ {channel.mention} добавлен в **{self.info['label']}**."
            else:
                ok = await remove_from_list(interaction.guild_id, self.key, channel.id)
                text = (
                    f"✅ {channel.mention} убран из **{self.info['label']}**."
                    if ok else "⚠️ Этого канала не было в списке."
                )
        else:
            await set_value(interaction.guild_id, self.key, channel.id)
            text = f"✅ **{self.info['label']}** → {channel.mention}"
        await interaction.response.edit_message(content=text, view=None)


class _ManualIdModal(discord.ui.Modal):
    """Указать ID вручную (для объектов, которых нет в селекторе)."""

    def __init__(self, key: str, info: dict):
        super().__init__(title=f"ID: {info['label']}"[:45])
        self.key = key
        self.info = info
        self.id_input = discord.ui.TextInput(
            label="ID (число)", placeholder="например 123456789012345678", required=True, max_length=25
        )
        self.add_item(self.id_input)

    async def on_submit(self, interaction: discord.Interaction):
        value = self.id_input.value.strip()
        if not value.isdigit():
            await interaction.response.send_message("⚠️ ID должен быть числом.", ephemeral=True)
            return

        if self.info["is_list"]:
            await add_to_list(interaction.guild_id, self.key, int(value))
            text = f"✅ `{value}` добавлен(а) в **{self.info['label']}**."
        else:
            await set_value(interaction.guild_id, self.key, value)
            text = f"✅ **{self.info['label']}** → `{value}`"
        await interaction.response.send_message(text, ephemeral=True)


class _TextEditModal(discord.ui.Modal):
    """Редактирование текстового значения конфига."""
    def __init__(self, key: str, info: dict, current: str = ""):
        super().__init__(title=f"Изменить: {info['label'][:40]}")
        self.key = key
        self.info = info
        self.text_input = discord.ui.TextInput(
            label=info["label"][:45],
            default=current[:4000],
            required=True,
            max_length=2000,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        await set_value(interaction.guild_id, self.key, self.text_input.value)
        await interaction.response.send_message(
            f"✅ **{self.info['label']}** обновлено.", ephemeral=True
        )


class _ValueActionView(discord.ui.View):
    """Второй экран: кнопки/селекторы действий для выбранного ключа."""

    def __init__(self, key: str, info: dict, current_text: str = ""):
        super().__init__(timeout=120)
        self.key = key
        self.info = info
        self.current_text = current_text

        if info["type"] == "text":
            pass  # только кнопки ниже
        elif info["is_list"]:
            if info["type"] == "role":
                self.add_item(_RoleSetSelect(key, info, add=True))
                self.add_item(_RoleSetSelect(key, info, add=False))
            else:
                self.add_item(_ChannelSetSelect(key, info, add=True))
                self.add_item(_ChannelSetSelect(key, info, add=False))
        else:
            if info["type"] == "role":
                self.add_item(_RoleSetSelect(key, info, add=True))
            else:
                self.add_item(_ChannelSetSelect(key, info, add=True))

    @discord.ui.button(label="✏️ Изменить текст", style=discord.ButtonStyle.primary, row=2)
    async def edit_text(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.info["type"] != "text":
            await interaction.response.send_modal(_ManualIdModal(self.key, self.info))
        else:
            await interaction.response.send_modal(
                _TextEditModal(self.key, self.info, self.current_text)
            )

    @discord.ui.button(label="🗑️ Сбросить", style=discord.ButtonStyle.danger, row=2)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        await clear_value(interaction.guild_id, self.key)
        await interaction.response.edit_message(
            content=f"✅ Настройка **{self.info['label']}** сброшена.", view=None
        )


# ════════════════════════════════════════════════
#  UI: ПЕРВЫЙ ШАГ — ВЫБОР ПАРАМЕТРА (ГЛАВНОЕ ДРОПМЕНЮ)
# ════════════════════════════════════════════════

class _ConfigKeySelect(discord.ui.Select):
    def __init__(self, page: int = 0):
        self.page = page
        items = list(CONFIG_KEYS.items())
        chunk = items[page * 25:(page + 1) * 25]

        options = [
            discord.SelectOption(label=info["label"][:100], value=key, description=key[:100])
            for key, info in chunk
        ]
        super().__init__(placeholder="Выбери, что настроить...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        info = CONFIG_KEYS[key]
        status = await _format_status(interaction.guild_id, key, info)
        current_text = ""
        if info["type"] == "text":
            current_text = await get_value(interaction.guild_id, key) or ""

        embed = discord.Embed(
            title=f"⚙️ {info['label']}",
            description=f"Текущее значение: {status}",
            color=discord.Color.blurple(),
        )
        view = _ValueActionView(key, info, current_text)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ConfigMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        # Discord допускает максимум 25 опций в одном select.
        # Текущих ключей < 25, поэтому одной страницы достаточно.
        self.add_item(_ConfigKeySelect(page=0))

        extra = list(CONFIG_KEYS.items())[25:]
        if extra:
            self.add_item(_ConfigKeySelect(page=1))


# ════════════════════════════════════════════════
#  COG
# ════════════════════════════════════════════════

class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="config", description="[ADMIN] Настроить роли/каналы бота через меню")
    @app_commands.checks.has_permissions(administrator=True)
    async def config(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="⚙️ Настройки бота",
            description="Выбери параметр в меню ниже, чтобы посмотреть и изменить его значение.",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, view=ConfigMainView(), ephemeral=True)

    @app_commands.command(name="config_show", description="[ADMIN] Показать текущие настройки бота")
    @app_commands.checks.has_permissions(administrator=True)
    async def config_show(self, interaction: discord.Interaction):
        embed = discord.Embed(title="⚙️ Настройки бота", color=discord.Color.blurple())

        lines = []
        for key, info in CONFIG_KEYS.items():
            status = await _format_status(interaction.guild_id, key, info)
            dot = "⚪" if status in ("не настроено", "пусто") else "🟢"
            lines.append(f"{dot} **{info['label']}** — {status}")

        text = "\n".join(lines)
        if len(text) > 4000:
            text = text[:4000] + "\n…"
        embed.description = text
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCog(bot))
