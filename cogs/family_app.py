import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import asyncio
from datetime import datetime

from cogs.config import get_id, get_value, set_value

DB = "bot.db"


# ════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════

async def _send_log(guild: discord.Guild, embed: discord.Embed):
    """Отправить embed в канал логов заявок."""
    log_ch_id = await get_id(guild.id, "app_log_channel")
    if log_ch_id:
        ch = guild.get_channel(log_ch_id)
        if ch:
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass


async def _get_panel_text(guild_id: int) -> tuple[str, str]:
    """Возвращает (title, description) для панели заявок из БД или дефолт."""
    title = await get_value(guild_id, "app_panel_title") or "🏠 Заявка в семью"
    desc  = await get_value(guild_id, "app_panel_desc")  or (
        "Хочешь вступить в нашу семью?\n\n"
        "Нажми на кнопку ниже, заполни форму и дождись решения HR.\n\n"
        "**Требования:**\n"
        "• Активность в голосовых каналах\n"
        "• Знание игры\n"
        "• Адекватное поведение"
    )
    return title, desc


# ════════════════════════════════════════════════
#  MODAL — форма заявки
# ════════════════════════════════════════════════

class FamilyAppModal(discord.ui.Modal, title="Подать заявку"):

    name_nick = discord.ui.TextInput(
        label="Ваше имя | ник",
        placeholder="Дмитрий | Viktor Pavlovian",
        max_length=100,
    )
    play_time = discord.ui.TextInput(
        label="Сколько проводите время в игре?",
        placeholder="3-5 часов",
        max_length=100,
    )
    age = discord.ui.TextInput(
        label="Ваш возраст?",
        placeholder="18",
        max_length=20,
    )
    shooting_recall = discord.ui.TextInput(
        label="Откат стрельбы",
        placeholder="откат стрельбы только DM на арене",
        max_length=200,
        style=discord.TextStyle.short,
    )
    how_found = discord.ui.TextInput(
        label="Как узнали о семье",
        placeholder="Друг посоветовал",
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        user  = interaction.user

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """INSERT INTO family_apps
                   (user_id, name_nick, play_time, age, shooting_recall, how_found, status, created_at)
                   VALUES (?,?,?,?,?,?,'pending',CURRENT_TIMESTAMP)""",
                (user.id, self.name_nick.value, self.play_time.value,
                 self.age.value, self.shooting_recall.value, self.how_found.value),
            )
            await db.commit()

            async with db.execute(
                "SELECT COUNT(*) FROM family_apps WHERE user_id=? AND status!='pending'",
                (user.id,),
            ) as cur:
                prev_count = (await cur.fetchone())[0]

        # Embed заявки
        embed = discord.Embed(
            title="📋 Заявление",
            color=discord.Color.from_rgb(44, 47, 51),
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Ваше имя | ник",                 value=self.name_nick.value,       inline=False)
        embed.add_field(name="Сколько проводите время в игре?", value=self.play_time.value,       inline=False)
        embed.add_field(name="Ваш возраст?",                   value=self.age.value,             inline=False)
        embed.add_field(name="Откат стрельбы",                 value=self.shooting_recall.value, inline=False)
        embed.add_field(name="Как узнали о семье",             value=self.how_found.value,       inline=False)
        embed.add_field(name="Пользователь",                   value=user.mention,               inline=False)
        embed.add_field(name="Username",                       value=user.name,                  inline=False)
        embed.add_field(name="ID",                             value=str(user.id),               inline=False)
        embed.set_footer(text=f"Сегодня, в {datetime.utcnow().strftime('%H:%M')}")

        prev_text = f"Предыдущих заявок: {prev_count}" if prev_count else "Предыдущих заявок: Заявок не найдено."

        # Получаем настройки
        dep_owner_role_id = await get_id(guild.id, "dep_owner_role_id")
        recruit_role_id   = await get_id(guild.id, "recruit_role_id")
        hr_role_id        = await get_id(guild.id, "hr_role_id")
        app_category_id   = await get_id(guild.id, "app_category_id")
        # Пинг при создании тикета — роль или юзер
        app_ping_id       = await get_value(guild.id, "app_ping_mention")

        # ── Создаём приватный канал ──
        category = guild.get_channel(app_category_id) if app_category_id else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
        }
        for rid in [dep_owner_role_id, recruit_role_id, hr_role_id]:
            if rid:
                role = guild.get_role(rid)
                if role:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, read_message_history=True
                    )

        channel_name = f"заявка-{user.name[:20]}"
        app_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Заявка от {user}",
        )

        # Формируем строку пинга
        ping_parts = []
        if app_ping_id and app_ping_id.strip():
            for pid in app_ping_id.split(","):
                pid = pid.strip()
                if pid.isdigit():
                    # Проверяем: роль или юзер
                    role_obj = guild.get_role(int(pid))
                    if role_obj:
                        ping_parts.append(f"<@&{pid}>")
                    else:
                        ping_parts.append(f"<@{pid}>")

        ping_str = " ".join(ping_parts) if ping_parts else ""
        content_parts = [prev_text]
        if ping_str:
            content_parts.append(ping_str)

        msg = await app_channel.send(
            content="\n".join(content_parts),
            embed=embed,
            view=AppReviewView(),
        )

        # Сохраняем channel_id и message_id
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """UPDATE family_apps SET review_msg_id=?, app_channel_id=? WHERE id = (
                       SELECT id FROM family_apps WHERE user_id=? AND status='pending'
                       ORDER BY id DESC LIMIT 1
                   )""",
                (msg.id, app_channel.id, user.id),
            )
            await db.commit()

        # Дублируем в review-канал
        app_review_channel = await get_id(guild.id, "app_review_channel")
        review_ch = guild.get_channel(app_review_channel) if app_review_channel else None
        if review_ch:
            rc_content = f"{prev_text}\n📁 Канал заявки: {app_channel.mention}"
            if ping_str:
                rc_content = f"{prev_text}\n{ping_str}\n📁 Канал заявки: {app_channel.mention}"
            await review_ch.send(content=rc_content, embed=embed)

        # Лог — новая заявка
        log_embed = discord.Embed(
            title="📥 Новая заявка",
            color=discord.Color.blurple(),
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(name="Заявитель", value=f"{user.mention} (`{user.name}`)", inline=True)
        log_embed.add_field(name="Канал",     value=app_channel.mention,               inline=True)
        await _send_log(guild, log_embed)

        await interaction.followup.send(
            f"✅ Твоя заявка отправлена! Канал: {app_channel.mention}\nОжидай решения.",
            ephemeral=True,
        )


# ════════════════════════════════════════════════
#  MODALS — действия HR
# ════════════════════════════════════════════════

class RejectModal(discord.ui.Modal, title="Отклонение заявки"):
    reason = discord.ui.TextInput(
        label="Причина отклонения",
        placeholder="Например: недостаточно активности",
        required=True,
        max_length=500,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, app_user_id: int, original_msg: discord.Message):
        super().__init__()
        self.app_user_id  = app_user_id
        self.original_msg = original_msg

    async def on_submit(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                """SELECT app_channel_id FROM family_apps WHERE user_id=? AND status='pending'
                   ORDER BY id DESC LIMIT 1""",
                (self.app_user_id,),
            ) as cur:
                row = await cur.fetchone()
            app_channel_id = row[0] if row else None

            await db.execute(
                """UPDATE family_apps SET status='rejected' WHERE id = (
                       SELECT id FROM family_apps WHERE user_id=? AND status='pending'
                       ORDER BY id DESC LIMIT 1
                   )""",
                (self.app_user_id,),
            )
            await db.commit()

        member = interaction.guild.get_member(self.app_user_id)

        # ЛС заявителю
        try:
            if member:
                dm_embed = discord.Embed(
                    title="❌ Заявка отклонена",
                    description=(
                        "К сожалению, твоя заявка в семью была **отклонена**.\n\n"
                        f"📝 **Причина:** {self.reason.value}\n\n"
                        "Попробуй снова позже."
                    ),
                    color=discord.Color.red(),
                )
                await member.send(embed=dm_embed)
        except discord.Forbidden:
            pass

        # Обновляем embed
        new_embed = self.original_msg.embeds[0].copy()
        new_embed.color = discord.Color.red()
        new_embed.set_footer(text=f"❌ Отклонил — {interaction.user.display_name} • {datetime.utcnow().strftime('%H:%M')}")
        new_embed.add_field(name="Отклонил",         value=interaction.user.mention, inline=True)
        new_embed.add_field(name="Причина",          value=self.reason.value,        inline=True)
        await self.original_msg.edit(embed=new_embed, view=None)

        # Лог
        log_embed = discord.Embed(
            title="❌ Заявка отклонена",
            color=discord.Color.red(),
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(name="Заявитель",  value=f"{member.mention if member else self.app_user_id}", inline=True)
        log_embed.add_field(name="Модератор",  value=interaction.user.mention,                            inline=True)
        log_embed.add_field(name="Причина",    value=self.reason.value,                                   inline=False)
        await _send_log(interaction.guild, log_embed)

        # Закрываем канал
        if app_channel_id:
            app_ch = interaction.guild.get_channel(app_channel_id)
            if app_ch:
                await app_ch.send(
                    embed=discord.Embed(
                        description=f"❌ Заявка **отклонена**.\n📝 Причина: {self.reason.value}\n\nКанал будет удалён через 10 секунд.",
                        color=discord.Color.red(),
                    )
                )
                await asyncio.sleep(10)
                await app_ch.delete(reason="Заявка отклонена")

        await interaction.response.send_message("❌ Заявка отклонена, заявитель уведомлён.", ephemeral=True)


class CallbackModal(discord.ui.Modal, title="Вызов на обзвон"):
    note = discord.ui.TextInput(
        label="Дополнительная информация",
        placeholder="Например: вызвать в 20:00",
        required=False,
        max_length=300,
    )

    def __init__(self, app_user_id: int, original_msg: discord.Message):
        super().__init__()
        self.app_user_id  = app_user_id
        self.original_msg = original_msg

    async def on_submit(self, interaction: discord.Interaction):
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """UPDATE family_apps SET status='callback' WHERE id = (
                       SELECT id FROM family_apps WHERE user_id=? AND status='pending'
                       ORDER BY id DESC LIMIT 1
                   )""",
                (self.app_user_id,),
            )
            await db.commit()

        member = interaction.guild.get_member(self.app_user_id)
        try:
            if member:
                dm_embed = discord.Embed(
                    title="📞 Вызов на обзвон",
                    description=(
                        "Твоя заявка в семью рассматривается.\n"
                        "Тебя вызывают на обзвон!"
                        + (f"\n\n📝 {self.note.value}" if self.note.value else "")
                    ),
                    color=discord.Color.blue(),
                )
                await member.send(embed=dm_embed)
        except discord.Forbidden:
            pass

        new_embed = self.original_msg.embeds[0].copy()
        new_embed.color = discord.Color.blue()
        new_embed.set_footer(text=f"📞 Вызван на обзвон — {interaction.user.display_name}")
        await self.original_msg.edit(embed=new_embed)

        # Лог
        log_embed = discord.Embed(
            title="📞 Вызов на обзвон",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(name="Заявитель", value=f"{member.mention if member else self.app_user_id}", inline=True)
        log_embed.add_field(name="Модератор", value=interaction.user.mention,                            inline=True)
        if self.note.value:
            log_embed.add_field(name="Заметка", value=self.note.value, inline=False)
        await _send_log(interaction.guild, log_embed)

        await interaction.response.send_message("✅ Заявитель вызван на обзвон и уведомлён.", ephemeral=True)


# ════════════════════════════════════════════════
#  MODAL — редактирование текста панели
# ════════════════════════════════════════════════

class EditPanelModal(discord.ui.Modal, title="Редактировать панель заявок"):
    panel_title = discord.ui.TextInput(
        label="Заголовок",
        placeholder="🏠 Заявка в семью",
        required=True,
        max_length=100,
    )
    panel_desc = discord.ui.TextInput(
        label="Текст описания",
        style=discord.TextStyle.paragraph,
        placeholder="Опиши требования и правила...",
        required=True,
        max_length=2000,
    )

    def __init__(self, guild_id: int, panel_msg: discord.Message | None = None):
        super().__init__()
        self.guild_id  = guild_id
        self.panel_msg = panel_msg  # если передать — обновит существующее сообщение

    async def on_submit(self, interaction: discord.Interaction):
        await set_value(self.guild_id, "app_panel_title", self.panel_title.value)
        await set_value(self.guild_id, "app_panel_desc",  self.panel_desc.value)

        # Если передали сообщение — обновляем его на месте
        if self.panel_msg:
            try:
                new_embed = discord.Embed(
                    title=self.panel_title.value,
                    description=self.panel_desc.value,
                    color=discord.Color.gold(),
                )
                new_embed.set_footer(text="ETERNAL HELPER • Заявки в семью")
                await self.panel_msg.edit(embed=new_embed)
                await interaction.response.send_message(
                    "✅ Текст панели обновлён прямо в сообщении!", ephemeral=True
                )
                return
            except discord.Forbidden:
                pass

        await interaction.response.send_message(
            "✅ Текст сохранён. При следующем `/app_panel` будет использоваться новый текст.\n"
            "Чтобы обновить уже отправленную панель — используй `/app_panel_edit` с указанием сообщения.",
            ephemeral=True,
        )


# ════════════════════════════════════════════════
#  VIEWS
# ════════════════════════════════════════════════

class AppReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _get_user_id(self, embed: discord.Embed) -> int | None:
        for field in embed.fields:
            if field.name == "ID" and field.value:
                val = field.value.strip()
                if val.isdigit():
                    return int(val)
        return None

    async def _is_hr(self, interaction: discord.Interaction) -> bool:
        if interaction.user.guild_permissions.administrator:
            return True
        role_ids = {r.id for r in interaction.user.roles}
        hr_role_id        = await get_id(interaction.guild_id, "hr_role_id")
        dep_owner_role_id = await get_id(interaction.guild_id, "dep_owner_role_id")
        return (
            (bool(hr_role_id) and hr_role_id in role_ids)
            or (bool(dep_owner_role_id) and dep_owner_role_id in role_ids)
        )

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success, custom_id="app_accept", emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        uid = self._get_user_id(interaction.message.embeds[0]) if interaction.message.embeds else None
        if not uid:
            await interaction.response.send_message("⚠️ Не удалось определить пользователя.", ephemeral=True)
            return

        member = interaction.guild.get_member(uid)

        # Роль
        member_role_id = await get_id(interaction.guild_id, "member_role_id")
        if member_role_id and member:
            role = interaction.guild.get_role(member_role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Принят в семью")
                except discord.Forbidden:
                    pass

        # ЛС
        try:
            if member:
                await member.send(embed=discord.Embed(
                    title="✅ Заявка принята!",
                    description="Поздравляем! Твоя заявка в семью была **принята**. Добро пожаловать!",
                    color=discord.Color.green(),
                ))
        except discord.Forbidden:
            pass

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                """SELECT app_channel_id FROM family_apps WHERE user_id=? AND status='pending'
                   ORDER BY id DESC LIMIT 1""",
                (uid,),
            ) as cur:
                row = await cur.fetchone()
            app_channel_id = row[0] if row else None

            await db.execute(
                """UPDATE family_apps SET status='accepted' WHERE id = (
                       SELECT id FROM family_apps WHERE user_id=? AND status='pending'
                       ORDER BY id DESC LIMIT 1
                   )""",
                (uid,),
            )
            await db.commit()

        new_embed = interaction.message.embeds[0].copy()
        new_embed.color = discord.Color.green()
        new_embed.set_footer(text=f"✅ Принял — {interaction.user.display_name} • {datetime.utcnow().strftime('%H:%M')}")
        new_embed.add_field(name="Принял", value=interaction.user.mention, inline=True)
        await interaction.message.edit(embed=new_embed, view=None)

        # Лог
        log_embed = discord.Embed(
            title="✅ Заявка принята",
            color=discord.Color.green(),
            timestamp=datetime.utcnow(),
        )
        log_embed.add_field(name="Заявитель", value=f"{member.mention if member else uid}", inline=True)
        log_embed.add_field(name="Модератор", value=interaction.user.mention,               inline=True)
        await _send_log(interaction.guild, log_embed)

        # Закрываем канал
        if app_channel_id:
            app_ch = interaction.guild.get_channel(app_channel_id)
            if app_ch:
                await app_ch.send(embed=discord.Embed(
                    description="✅ Заявка **принята**! Добро пожаловать в семью!\n\nКанал будет удалён через 10 секунд.",
                    color=discord.Color.green(),
                ))
                await asyncio.sleep(10)
                await app_ch.delete(reason="Заявка принята")

        await interaction.response.send_message("✅ Заявка принята. Роль выдана.", ephemeral=True)

    @discord.ui.button(label="На рассмотрение", style=discord.ButtonStyle.primary, custom_id="app_review", emoji="👁️")
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        new_embed = interaction.message.embeds[0].copy()
        new_embed.color = discord.Color.yellow()
        new_embed.set_footer(text=f"👁️ На рассмотрении — {interaction.user.display_name}")
        await interaction.message.edit(embed=new_embed)

        # Лог
        log_embed = discord.Embed(
            title="👁️ Заявка на рассмотрении",
            color=discord.Color.yellow(),
            timestamp=datetime.utcnow(),
        )
        uid = self._get_user_id(interaction.message.embeds[0])
        member = interaction.guild.get_member(uid) if uid else None
        log_embed.add_field(name="Заявитель", value=f"{member.mention if member else uid}", inline=True)
        log_embed.add_field(name="Модератор", value=interaction.user.mention,               inline=True)
        await _send_log(interaction.guild, log_embed)

        await interaction.response.send_message("👁️ Заявка взята на рассмотрение.", ephemeral=True)

    @discord.ui.button(label="Вызвать на обзвон", style=discord.ButtonStyle.secondary, custom_id="app_callback", emoji="📞")
    async def callback(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        uid = self._get_user_id(interaction.message.embeds[0]) if interaction.message.embeds else None
        if not uid:
            await interaction.response.send_message("⚠️ Не удалось определить пользователя.", ephemeral=True)
            return

        await interaction.response.send_modal(CallbackModal(uid, interaction.message))

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, custom_id="app_reject", emoji="❌")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        uid = self._get_user_id(interaction.message.embeds[0]) if interaction.message.embeds else None
        if not uid:
            await interaction.response.send_message("⚠️ Не удалось определить пользователя.", ephemeral=True)
            return

        await interaction.response.send_modal(RejectModal(uid, interaction.message))


class AppPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📋 Подать заявку",
        style=discord.ButtonStyle.primary,
        custom_id="family_app_open",
    )
    async def open_app(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT id FROM family_apps WHERE user_id=? AND status='pending'",
                (interaction.user.id,),
            ) as cur:
                pending = await cur.fetchone()

        if pending:
            await interaction.response.send_message(
                "⚠️ У тебя уже есть заявка на рассмотрении. Дождись ответа!", ephemeral=True
            )
            return

        await interaction.response.send_modal(FamilyAppModal())


# ════════════════════════════════════════════════
#  COG
# ════════════════════════════════════════════════

class FamilyApp(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(AppPanelView())
        bot.add_view(AppReviewView())

    @app_commands.command(name="app_panel", description="[ADMIN] Отправить панель заявки в семью")
    @app_commands.checks.has_permissions(administrator=True)
    async def app_panel(self, interaction: discord.Interaction):
        title, desc = await _get_panel_text(interaction.guild_id)
        embed = discord.Embed(title=title, description=desc, color=discord.Color.gold())
        embed.set_footer(text="ETERNAL HELPER • Заявки в семью")
        await interaction.channel.send(embed=embed, view=AppPanelView())
        await interaction.response.send_message("✅ Панель заявок отправлена!", ephemeral=True)

    @app_commands.command(name="app_panel_edit", description="[ADMIN] Изменить текст панели заявок")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(message_id="ID сообщения с панелью (чтобы обновить на месте, необязательно)")
    async def app_panel_edit(self, interaction: discord.Interaction, message_id: str = None):
        panel_msg = None
        if message_id:
            try:
                panel_msg = await interaction.channel.fetch_message(int(message_id))
            except (discord.NotFound, ValueError):
                await interaction.response.send_message("⚠️ Сообщение не найдено в этом канале.", ephemeral=True)
                return

        # Подставляем текущие значения в модалку
        title, desc = await _get_panel_text(interaction.guild_id)
        modal = EditPanelModal(interaction.guild_id, panel_msg)
        modal.panel_title.default = title
        modal.panel_desc.default  = desc
        await interaction.response.send_modal(modal)

    @app_commands.command(name="app_list", description="[HR] Список заявок")
    async def app_list(self, interaction: discord.Interaction):
        role_ids = {r.id for r in interaction.user.roles}
        hr_role_id        = await get_id(interaction.guild_id, "hr_role_id")
        dep_owner_role_id = await get_id(interaction.guild_id, "dep_owner_role_id")
        is_hr = (
            (bool(hr_role_id) and hr_role_id in role_ids)
            or (bool(dep_owner_role_id) and dep_owner_role_id in role_ids)
            or interaction.user.guild_permissions.administrator
        )
        if not is_hr:
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id, name_nick, status, created_at FROM family_apps ORDER BY created_at DESC LIMIT 20"
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message("📭 Заявок нет.", ephemeral=True)
            return

        status_emoji = {"pending": "⏳", "accepted": "✅", "rejected": "❌", "callback": "📞"}
        lines = [
            f"{status_emoji.get(s, '❓')} **{nick}** — <@{uid}> ({s})"
            for uid, nick, s, _ in rows
        ]
        embed = discord.Embed(
            title="📋 Заявки в семью",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(FamilyApp(bot))
