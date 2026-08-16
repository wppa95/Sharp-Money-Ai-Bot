import asyncio
from config import config
from database import Database

async def main():
    db = Database(config.DATABASE_URL)
    await db.init()
    n = await db.count_player_results()
    print("player_game_results count:", n)

asyncio.run(main())