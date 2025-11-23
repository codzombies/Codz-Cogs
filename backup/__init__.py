# SPDX-FileCopyrightText: 2025 cswimr <copyright@csw.im>
# SPDX-License-Identifier: MPL-2.0

from .backup import Backup


async def setup(bot) -> None:
    await bot.add_cog(Backup(bot))
