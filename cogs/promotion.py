import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
from datetime import datetime

from cogs.config import get_id

# ──────────────────────────────────────────────
#  CONFIG — настраивается командами /config:
#   hr_role_id        — роль HR
#   admin_role_id     — роль Admin
#   promo_log_channel — канал для логов заявок на повышение
# ───────────────────────────────────────────────

DB = "bot.db"


async def _is_hr(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in interaction.user.roles}
    hr_role_id = await get_id(interaction.guild_id, "hr_role_id")
    admin_role_id = await get_id(interaction.guild_id, "admin_role_id")
    return (bool(hr_role_id) and hr_role_id in role_ids) or (bool(admin_role_id) and admin_role_id in role_ids)


# ════════════════════════════════════════════════
#  MODAL — заявка на повышение
# ════════════════════════════════════════════════

class PromotionModal(discord.ui.Modal, title="Заявка на повышение"):
    current_rank = discord.ui.TextInput(
        label="Текущий ранг / должность",
        placeholder="Например: Recruit",
        max_length=100,
    )
    desired_rank = discord.ui.TextInput(
        label="Желаемый ранг",
        placeholder="Например: Member",
        max_length=100,
    )
    activity = discord.ui.TextInput(
        label="Активность (часы в войсе, дела)",
        placeholder="10 часов в войсе, участвовал в 3 МП...",
        style=discord.TextStyle.long,
        max_length=500,
    )
    reason = discord.ui.TextInput(
        label="Почему заслуживаешь повышения?",
        style=discord.TextStyle.long,
        placeholder="Опиши свои заслуги...",
        max_length=800,
    )

    def __init__(self, target_role: discord.Role | None = None):
        super().__init__()
        self.target_role = target_role

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Сохранить в БД
        async with aiosqlite.connect(DB) as db:
            await db.execute(
                """INSERT INTO promotions (user_id, current_rank, desired_rank, activity, reason, status, created_at)
                   VALUES (?,?,?,?,?,'pending',CURRENT_TIMESTAMP)""",
                (
                    interaction.user.id,
                    self.current_rank.value,
                    self.desired_rank.value,
                    self.activity.value,
                    self.reason.value,
                ),
            )
            await db.commit()

        embed = discord.Embed(
            title="📈 Заявка на повышение",
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )
        embed.add_field(name="Участник",         value=interaction.user.mention, inline=True)
        embed.add_field(name="Username / ID",    value=f"{interaction.user.name} / {interaction.user.id}", inline=True)
        embed.add_field(name="Текущий ранг",     value=self.current_rank.value,  inline=False)
        embed.add_field(name="Желаемый ранг",    value=self.desired_rank.value,  inline=False)
        embed.add_field(name="Активность",       value=self.activity.value,      inline=False)
        embed.add_field(name="Причина",          value=self.reason.value,        inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.set_footer(text="Заявка на повышение")

        # Пинговать HR если настроено
        hr_role_id = await get_id(interaction.guild_id, "hr_role_id")
        promo_log_channel = await get_id(interaction.guild_id, "promo_log_channel")
        ping = f"<@&{hr_role_id}>" if hr_role_id else ""

        if promo_log_channel:
            log_ch = interaction.guild.get_channel(promo_log_channel)
            if log_ch:
                await log_ch.send(
                    content=ping,
                    embed=embed,
                    view=PromoReviewView(interaction.user.id),
                )

        await interaction.followup.send(
            "✅ Заявка на повышение отправлена! HR рассмотрит её в ближайшее время.",
            ephemeral=True,
        )


# ════════════════════════════════════════════════
#  VIEW — кнопки HR для рассмотрения заявки
# ════════════════════════════════════════════════

class PromoReviewView(discord.ui.View):
    def __init__(self, applicant_id: int = 0):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id

    def _get_uid(self, embed: discord.Embed) -> int | None:
        for field in embed.fields:
            if "ID" in field.name and field.value:
                for part in field.value.split():
                    if part.isdigit() and len(part) > 5:
                        return int(part)
        return None

    @discord.ui.button(label="✅ Одобрить", style=discord.ButtonStyle.success, custom_id="promo_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        uid = self._get_uid(interaction.message.embeds[0]) if interaction.message.embeds else self.applicant_id
        if uid:
            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    "UPDATE promotions SET status='approved' WHERE user_id=? AND status='pending'", (uid,)
                )
                await db.commit()
            try:
                member = interaction.guild.get_member(uid)
                if member:
                    await member.send(
                        embed=discord.Embed(
                            title="✅ Заявка на повышение одобрена!",
                            description="Поздравляем! Твоя заявка на повышение была **одобрена**.",
                            color=discord.Color.green(),
                        )
                    )
            except discord.Forbidden:
                pass

        new_embed = interaction.message.embeds[0].copy()
        new_embed.color = discord.Color.green()
        new_embed.set_footer(text=f"✅ Одобрено — {interaction.user.display_name}")
        await interaction.message.edit(embed=new_embed, view=None)
        await interaction.response.send_message("✅ Заявка одобрена.", ephemeral=True)

    @discord.ui.button(label="❌ Отклонить", style=discord.ButtonStyle.danger, custom_id="promo_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        uid = self._get_uid(interaction.message.embeds[0]) if interaction.message.embeds else self.applicant_id
        if uid:
            async with aiosqlite.connect(DB) as db:
                await db.execute(
                    "UPDATE promotions SET status='rejected' WHERE user_id=? AND status='pending'", (uid,)
                )
                await db.commit()
            try:
                member = interaction.guild.get_member(uid)
                if member:
                    await member.send(
                        embed=discord.Embed(
                            title="❌ Заявка на повышение отклонена",
                            description="К сожалению, твоя заявка на повышение была **отклонена**.\nСтарайся больше!",
                            color=discord.Color.red(),
                        )
                    )
            except discord.Forbidden:
                pass

        new_embed = interaction.message.embeds[0].copy()
        new_embed.color = discord.Color.red()
        new_embed.set_footer(text=f"❌ Отклонено — {interaction.user.display_name}")
        await interaction.message.edit(embed=new_embed, view=None)
        await interaction.response.send_message("❌ Заявка отклонена.", ephemeral=True)

    @discord.ui.button(label="👁️ На рассмотрении", style=discord.ButtonStyle.primary, custom_id="promo_pending")
    async def pending(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        new_embed = interaction.message.embeds[0].copy()
        new_embed.color = discord.Color.yellow()
        new_embed.set_footer(text=f"👁️ Рассматривает — {interaction.user.display_name}")
        await interaction.message.edit(embed=new_embed)
        await interaction.response.send_message("👁️ Заявка взята на рассмотрение.", ephemeral=True)


# ════════════════════════════════════════════════
#  COG
# ════════════════════════════════════════════════

class PromotionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(PromoReviewView())

    @app_commands.command(name="promote", description="Подать заявку на повышение")
    async def promote_cmd(self, interaction: discord.Interaction):
        # Проверить нет ли уже активной заявки
        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT id FROM promotions WHERE user_id=? AND status='pending'",
                (interaction.user.id,),
            ) as cur:
                pending = await cur.fetchone()

        if pending:
            await interaction.response.send_message(
                "⚠️ У тебя уже есть заявка на рассмотрении. Подожди решения HR.", ephemeral=True
            )
            return

        await interaction.response.send_modal(PromotionModal())

    @app_commands.command(name="promote_send", description="[HR] Отправить заявку на повышение выбранной роли в ЛС")
    @app_commands.describe(role="Роль, которой отправить", message="Сообщение")
    async def promote_send(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        message: str,
    ):
        """Отправляет сообщение в ЛС всем с указанной ролью (например, призыв подать заявку)."""
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        members = [m for m in role.members if not m.bot]

        embed = discord.Embed(
            title=f"📨 Сообщение для {role.name}",
            description=message,
            color=discord.Color.blue(),
            timestamp=datetime.utcnow(),
        )
        embed.set_footer(text=f"От: {interaction.user.display_name} | {interaction.guild.name}")

        sent, failed = 0, 0
        for member in members:
            try:
                await member.send(embed=embed)
                sent += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1

        await interaction.followup.send(
            f"✅ Отправлено: **{sent}** / Ошибок: **{failed}**", ephemeral=True
        )

    @app_commands.command(name="promote_list", description="[HR] Список заявок на повышение")
    async def promote_list(self, interaction: discord.Interaction):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        async with aiosqlite.connect(DB) as db:
            async with db.execute(
                "SELECT user_id, current_rank, desired_rank, status, created_at FROM promotions ORDER BY created_at DESC LIMIT 15"
            ) as cur:
                rows = await cur.fetchall()

        if not rows:
            await interaction.response.send_message("📭 Заявок нет.", ephemeral=True)
            return

        status_emoji = {"pending": "⏳", "approved": "✅", "rejected": "❌"}
        lines = [
            f"{status_emoji.get(s,'❓')} <@{uid}> | {cur_r} → {des_r}"
            for uid, cur_r, des_r, s, _ in rows
        ]
        embed = discord.Embed(
            title="📈 Заявки на повышение",
            description="\n".join(lines),
            color=discord.Color.blue(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PromotionCog(bot))