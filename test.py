import asyncio
from wakapi_anyide._watch import Watch

async def main():
    notify = Watch()
    notify.add_watch("./")
    
    async for evs in notify:
        for ev in evs:
            print(ev)
        
if __name__ == "__main__":
    asyncio.run(main())