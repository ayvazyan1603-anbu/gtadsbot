import discord
from discord.ext import commands
from discord import app_commands

from cogs.config import get_id, get_list

# ──────────────────────────────────────────────
#  CONFIG — настраивается командой /config:
#   "Роль HR" (hr_role_id), "Роль администратора" (admin_role_id) — кто может
#   выдавать/снимать роли через /role_give и /role_remove
#   "Роли, доступные для выдачи (HR-команды)" (allowed_roles) — какие роли
#   можно выдавать/снимать этими командами
# ──────────────────────────────────────────────


async def _is_hr(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    role_ids = {r.id for r in interaction.user.roles}
    hr_role_id = await get_id(interaction.guild_id, "hr_role_id")
    admin_role_id = await get_id(interaction.guild_id, "admin_role_id")
    return (bool(hr_role_id) and hr_role_id in role_ids) or (bool(admin_role_id) and admin_role_id in role_ids)


class RoleSelect(discord.ui.Select):
    """Динамический селект ролей."""

    def __init__(self, guild: discord.Guild, target: discord.Member, action: str, allowed_role_ids: list[int]):
        self.target = target
        self.action = action  # "add" or "remove"

        options = []
        for role_id in allowed_role_ids:
            role = guild.get_role(role_id)
            if role:
                options.append(
                    discord.SelectOption(
                        label=role.name,
                        value=str(role.id),
                        emoji="✅" if role in target.roles else "➕",
                        description="Есть у участника" if role in target.roles else "Нет у участника",
                    )
                )

        if not options:
            options = [discord.SelectOption(label="Нет доступных ролей", value="none")]

        super().__init__(
            placeholder=f"Выбери роль для {'выдачи' if action == 'add' else 'снятия'}...",
            min_values=1,
            max_values=min(len(options), 5),
            options=options[:25],
            custom_id=f"role_select_{action}",
        )

    async def callback(self, interaction: discord.Interaction):
        if "none" in self.values:
            await interaction.response.send_message("⚠️ Нет доступных ролей.", ephemeral=True)
            return

        added, removed, failed = [], [], []
        for role_id_str in self.values:
            role = interaction.guild.get_role(int(role_id_str))
            if not role:
                continue
            try:
                if self.action == "add":
                    await self.target.add_roles(role, reason=f"Выдано {interaction.user}")
                    added.append(role.mention)
                else:
                    await self.target.remove_roles(role, reason=f"Снято {interaction.user}")
                    removed.append(role.mention)
            except discord.Forbidden:
                failed.append(role.name)

        lines = []
        if added:   lines.append(f"✅ Выдано: {', '.join(added)}")
        if removed: lines.append(f"🗑️ Снято: {', '.join(removed)}")
        if failed:  lines.append(f"❌ Ошибка (нет прав): {', '.join(failed)}")

        await interaction.response.send_message(
            "\n".join(lines) or "Ничего не изменилось.", ephemeral=True
        )


class RoleView(discord.ui.View):
    def __init__(self, guild: discord.Guild, target: discord.Member, action: str, allowed_role_ids: list[int]):
        super().__init__(timeout=60)
        self.add_item(RoleSelect(guild, target, action, allowed_role_ids))


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="role_give", description="[HR] Выдать роль участнику")
    @app_commands.describe(member="Участник")
    async def role_give(self, interaction: discord.Interaction, member: discord.Member):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        allowed_roles = await get_list(interaction.guild_id, "allowed_roles")
        if not allowed_roles:
            await interaction.response.send_message(
                "⚠️ Список доступных ролей пуст. Добавь роли командой `/config`.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"➕ Выдать роль → {member.display_name}",
            color=discord.Color.green(),
        )
        await interaction.response.send_message(
            embed=embed, view=RoleView(interaction.guild, member, "add", allowed_roles), ephemeral=True
        )

    @app_commands.command(name="role_remove", description="[HR] Снять роль с участника")
    @app_commands.describe(member="Участник")
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member):
        if not await _is_hr(interaction):
            await interaction.response.send_message("❌ Нет прав.", ephemeral=True)
            return

        allowed_roles = await get_list(interaction.guild_id, "allowed_roles")
        if not allowed_roles:
            await interaction.response.send_message(
                "⚠️ Список доступных ролей пуст. Добавь роли командой `/config`.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🗑️ Снять роль → {member.display_name}",
            color=discord.Color.red(),
        )
        await interaction.response.send_message(
            embed=embed, view=RoleView(interaction.guild, member, "remove", allowed_roles), ephemeral=True
        )

    @app_commands.command(name="role_info", description="Показать роли участника")
    @app_commands.describe(member="Участник (по умолчанию — ты)")
    async def role_info(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        roles = [r for r in reversed(target.roles) if r.name != "@everyone"]

        embed = discord.Embed(
            title=f"🎭 Роли — {target.display_name}",
            color=target.color,
        )
        if roles:
            embed.description = " ".join(r.mention for r in roles[:30])
            embed.set_footer(text=f"Всего ролей: {len(roles)}")
        else:
            embed.description = "Нет ролей"

        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RolesCog(bot))
