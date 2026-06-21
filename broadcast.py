import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
import io
from datetime import datetime

from cogs.config import get_id

# ──────────────────────────────────────────────
#  Все ID настраиваются командой /config (см. cogs/config.py)
#
#  Используемые этим когом настройки:
#   - "Категория для тикетов"        (ticket_category_id)
#   - "Канал логов тикетов"          (ticket_log_channel)
#   - "Роль поддержки (тикеты)"      (support_role_id)
#   - "Роль администратора"          (admin_role_id)
# ──────────────────────────────────────────────

TICKET_CATEGORIES = {
    "question":   {"label": "❓ Вопрос",          "emoji": "❓", "color": discord.Color.blue()},
    "complaint":  {"label": "📢 Жалоба",           "emoji": "📢", "color": discord.Color.red()},
    "promotion":  {"label": "📈 Повышение",         "emoji": "📈", "color": discord.Color.green()},
    "report":     {"label": "🚨 Репорт на игрока",  "emoji": "🚨", "color": discord.Color.orange()},
    "other":      {"label": "📝 Другое",            "emoji": "📝", "color": discord.Color.greyple()},
}

DB = "bot.db"


# ════════════════════════════════════════════════
#  PERSISTENT VIEWS
# ════════════════════════════════════════════════

class TicketCreateView(discord.ui.View):
    """Кнопки выбора категории тикета — отправляется в канал-панель."""

    def __init__(self):
        super().__init__(timeout=None)
        for key, info in TICKET_CATEGORIES.items():
            btn = discord.ui.Button(
                label=info["label"],
                emoji=info["emoji"],
                style=discord.ButtonStyle.secondary,
                custom_id=f"ticket_open_{key}",
            )
            btn.callback = self._make_callback(key)
            self.add_item(btn)

    def _make_callback(self, category_key: str):
        async def callback(interaction: discord.Interaction):
            await open_ticket(interaction, category_key)
        return callback


class TicketControlView(discord.ui.View):
    """Кнопки управления внутри канала тикета."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Взять тикет", style=discord.ButtonStyle.primary, custom_id="ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await claim_ticket(interaction)

    @discord.ui.button(label="🔒 Закрыть", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await close_ticket(interaction)

    @discord.ui.button(label="📋 Транскрипт", style=discord.ButtonStyle.secondary, custom_id="ticket_transcript")
    async def transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await send_transcript(interaction)


class ConfirmCloseView(discord.ui.View):
    """Подтверждение закрытия тикета."""

    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Да, закрыть", style=discord.ButtonStyle.danger, custom_id="ticket_close_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await _do_close_ticket(interaction)

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary, custom_id="ticket_close_cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ Закрытие отменено.", embed=None, view=None)


# ════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════

async def _has_support(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in interaction.user.roles}
    support_role_id = await get_id(interaction.guild_id, "support_role_id")
    admin_role_id = await get_id(interaction.guild_id, "admin_role_id")
    return (support_role_id and support_role_id in role_ids) or (admin_role_id and admin_role_id in role_ids)


async def open_ticket(interaction: discord.Interaction, category_key: str):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    user  = interaction.user
    info  = TICKET_CATEGORIES[category_key]

    # Проверяем, нет ли уже открытого тикета у пользователя
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT channel_id FROM tickets WHERE user_id=? AND status='open'", (user.id,)
        ) as cur:
            row = await cur.fetchone()
        if row:
            ch = guild.get_channel(row[0])
            mention = ch.mention if ch else "#удалён"
            await interaction.followup.send(
                f"⚠️ У тебя уже есть открытый тикет: {mention}", ephemeral=True
            )
            return

    # Категория и роль поддержки из конфигурации
    ticket_category_id = await get_id(guild.id, "ticket_category_id")
    support_role_id = await get_id(guild.id, "support_role_id")

    category = guild.get_channel(ticket_category_id) if ticket_category_id else None
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user:               discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if support_role_id:
        support_role = guild.get_role(support_role_id)
        if support_role:
            overwrites[support_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

    channel_name = f"ticket-{user.name[:15]}-{category_key}"
    channel = await guild.create_text_channel(
        name=channel_name,
        category=category,
        overwrites=overwrites,
        reason=f"Тикет от {user} — {category_key}",
    )

    # Пишем в БД
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO tickets (channel_id, user_id, category, status) VALUES (?,?,?,'open')",
            (channel.id, user.id, category_key),
        )
        await db.commit()

    # Embed внутри тикета
    embed = discord.Embed(
        title=f"{info['emoji']} Тикет — {info['label']}",
        description=(
            f"Привет, {user.mention}! 👋\n\n"
            f"Опиши свою проблему как можно подробнее.\n"
            f"Поддержка ответит в ближайшее время.\n\n"
            f"📅 Создан: <t:{int(datetime.utcnow().timestamp())}:F>"
        ),
        color=info["color"],
    )
    embed.set_footer(text="Используй кнопки ниже для управления тикетом.")

    await channel.send(
        content=f"{user.mention} | <@&{support_role_id}>" if support_role_id else user.mention,
        embed=embed,
        view=TicketControlView(),
    )

    await interaction.followup.send(
        f"✅ Твой тикет создан: {channel.mention}", ephemeral=True
    )


async def claim_ticket(interaction: discord.Interaction):
    if not await _has_support(interaction):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT user_id FROM tickets WHERE channel_id=? AND status='open'",
            (interaction.channel_id,),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await interaction.response.send_message("⚠️ Тикет не найден или уже закрыт.", ephemeral=True)
        return

    embed = discord.Embed(
        description=f"🎫 Тикет взят **{interaction.user.display_name}**",
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed)


async def close_ticket(interaction: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT user_id FROM tickets WHERE channel_id=? AND status='open'",
            (interaction.channel_id,),
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await interaction.response.send_message("⚠️ Это не активный тикет.", ephemeral=True)
        return

    is_owner   = interaction.user.id == row[0]
    is_support = await _has_support(interaction)

    if not (is_owner or is_support):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    embed = discord.Embed(
        title="🔒 Закрыть тикет?",
        description="Канал будет удалён. Это действие необратимо.",
        color=discord.Color.red(),
    )
    await interaction.response.send_message(embed=embed, view=ConfirmCloseView(), ephemeral=True)


async def _do_close_ticket(interaction: discord.Interaction):
    channel = interaction.channel
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "UPDATE tickets SET status='closed', closed_at=CURRENT_TIMESTAMP WHERE channel_id=?",
            (channel.id,),
        )
        await db.commit()

    # Лог в отдельный канал
    ticket_log_channel = await get_id(interaction.guild_id, "ticket_log_channel")
    if ticket_log_channel:
        log_ch = interaction.guild.get_channel(ticket_log_channel)
        if log_ch:
            embed = discord.Embed(
                title="📋 Тикет закрыт",
                description=f"Канал: `{channel.name}`\nЗакрыл: {interaction.user.mention}",
                color=discord.Color.dark_red(),
                timestamp=datetime.utcnow(),
            )
            await log_ch.send(embed=embed)

    await interaction.edit_original_response(content="🔒 Закрываю тикет...", embed=None, view=None)
    await asyncio.sleep(3)
    await channel.delete(reason=f"Тикет закрыт {interaction.user}")


async def send_transcript(interaction: discord.Interaction):
    if not await _has_support(interaction):
        await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    lines = []
    async for msg in channel.history(limit=500, oldest_first=True):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{ts}] {msg.author.display_name}: {msg.content}")

    text = "\n".join(lines) or "Нет сообщений."
    file = discord.File(
        fp=io.BytesIO(text.encode()),
        filename=f"transcript_{channel.name}.txt",
    )
    await interaction.followup.send("📋 Транскрипт:", file=file, ephemeral=True)


# ════════════════════════════════════════════════
#  COG
# ════════════════════════════════════════════════

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(TicketCreateView())
        bot.add_view(TicketControlView())

    # ── Admin команды ──────────────────────────

    @app_commands.command(name="ticket_panel", description="[ADMIN] Отправить панель создания тикетов")
    @app_commands.checks.has_permissions(administrator=True)
    async def ticket_panel(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎫 Служба поддержки",
            description=(
                "Нужна помощь? Открой тикет, нажав на нужную кнопку.\n\n"
                "❓ **Вопрос** — общий вопрос по организации\n"
                "📢 **Жалоба** — жалоба на участника\n"
                "📈 **Повышение** — заявка на повышение\n"
                "🚨 **Репорт** — репорт на игрока\n"
                "📝 **Другое** — всё остальное\n\n"
                "*Один пользователь — один открытый тикет.*"
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="ETERNAL HELPER • Служба поддержки")
        await interaction.channel.send(embed=embed, view=TicketCreateView())
        await interaction.response.send_message("✅ Панель отправлена!", ephemeral=True)




async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
