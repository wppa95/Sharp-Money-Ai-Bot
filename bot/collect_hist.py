import asyncio
from config import config
from database import Database

async def main():
    db = Database(config.DATABASE_URL)
    await db.init()
    snaps = await db.get_recent_underdog_snapshots(limit=50)
    print("ud_snaps:", len(snaps))
    if snaps:
        s = snaps[0]
        print("sample:", s.player_name, s.sport, s.stat_type, "removed=", getattr(s, "removed", None))
    try:
        n = await db.count_prop_line_history(provider="Underdog")
        print("prop_line_history Underdog:", n)
    except Exception as e:
        print("plh err:", type(e).__name__, e)
    try:
        pending = await db.get_pending_opportunities(cutoff_hours=0)
        print("opp_log sample count:", len(pending))
    except Exception as e:
        print("opp err:", type(e).__name__, e)
    await db.close()

asyncio.run(main())