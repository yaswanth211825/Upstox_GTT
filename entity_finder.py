import re
import logging
import traceback
from telethon.tl.types import Channel, Chat


class EntityFinder:
    """Simple reusable class to find a channel/group entity by a substring of its visible title.

    Usage:
        finder = EntityFinder()
        entity = await finder.find(client, 'MS-OPTIONS')

    The finder normalizes titles by removing non-alphanumeric chars and lowercasing so emojis/punctuation
    are ignored when matching.
    """

    def __init__(self, limit: int = 200):
        self.limit = limit

    @staticmethod
    def _normalize(s: str) -> str:
        if not s:
            return ""
        return re.sub(r"[^0-9a-zA-Z]+", "", s).lower()

    async def find(self, client, search_term: str):
        normalized_search = self._normalize(search_term)
        best_match = None
        try:
            logging.info("Fetching dialogs to locate target channel/group...")
            async for dialog in client.iter_dialogs(limit=self.limit):
                entity = dialog.entity
                if isinstance(entity, (Channel, Chat)):
                    title = getattr(entity, 'title', None) or str(dialog.name or '')
                    normalized_title = self._normalize(title)
                    logging.debug(f"Dialog: id={getattr(entity,'id',None)} title='{title}' normalized='{normalized_title}'")
                    if normalized_search and normalized_search in normalized_title:
                        logging.info(f"Found match: '{title}' (id={entity.id}). Selecting this as target.")
                        return entity
                    if best_match is None:
                        best_match = entity
        except Exception as e:
            logging.error(f"Error while iterating dialogs: {e}")
            logging.error(traceback.format_exc())

        if best_match:
            logging.warning("No exact title match found; using first discovered channel/group as a fallback. Please set GROUP_ENTITY_NAME env var to force a specific target.")
        else:
            logging.critical("No channels or groups found in account dialogs. Ensure the logged-in account is a member of channels/groups.")
        return best_match


async def resolve_entity(client, identifier: str):
    """Resolve an identifier to a Telegram entity.

    Behavior:
    - If identifier is all digits -> treat as numeric id and try client.get_entity(int(id)).
    - If identifier looks like a username (starts with @) or a t.me link -> pass to client.get_entity(identifier).
    - Otherwise, use EntityFinder to search dialogs by substring match.

    Returns the resolved entity or None.
    """
    if not identifier:
        return None

    stripped = identifier.strip()

    # numeric id -> direct get
    if re.fullmatch(r"\d+", stripped):
        try:
            logging.info(f"Trying to resolve numeric id: {stripped}")
            return await client.get_entity(int(stripped))
        except Exception:
            logging.debug("Numeric id lookup failed; will try dialog search as fallback.")

    # username or link -> direct get
    if stripped.startswith("@") or "t.me" in stripped or stripped.startswith("https://"):
        try:
            logging.info(f"Trying to resolve username/link: {stripped}")
            return await client.get_entity(stripped)
        except Exception:
            logging.debug("Direct username/link lookup failed; will try dialog search as fallback.")

    # fallback to searching dialogs by substring
    finder = EntityFinder()
    return await finder.find(client, stripped)


    
async def find_all_matches(client, search_term: str, limit: int = 200):
    """Return a list of candidate matches (entity, title, id, normalized_title) for the search_term."""
    finder = EntityFinder(limit=limit)
    normalized_search = finder._normalize(search_term)
    results = []
    try:
        async for dialog in client.iter_dialogs(limit=finder.limit):
            entity = dialog.entity
            if isinstance(entity, (Channel, Chat)):
                title = getattr(entity, 'title', None) or str(dialog.name or '')
                normalized_title = finder._normalize(title)
                if normalized_search and normalized_search in normalized_title:
                    results.append((entity, title, getattr(entity, 'id', None), normalized_title))
    except Exception as e:
        logging.error(f"Error while listing dialogs: {e}")
        logging.error(traceback.format_exc())
    return results


if __name__ == '__main__':
    # Simple CLI for discovery: run this module directly to list matching dialogs.
    # Reads TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_PHONE and GROUP_ENTITY_NAME from environment
    import os
    from dotenv import load_dotenv
    from telethon import TelegramClient

    load_dotenv()
    API_ID = int(os.getenv('TELEGRAM_API_ID', '0'))
    API_HASH = os.getenv('TELEGRAM_API_HASH')
    PHONE = os.getenv('TELEGRAM_PHONE')
    SEARCH = os.getenv('GROUP_ENTITY_NAME', None)

    async def _cli():
        if not all([API_ID, API_HASH]):
            print('Please set TELEGRAM_API_ID and TELEGRAM_API_HASH in your environment (or .env).')
            return
        client = TelegramClient('entity_finder_cli', API_ID, API_HASH)
        await client.start(phone=PHONE)
        if not SEARCH:
            print('Set GROUP_ENTITY_NAME in .env to a substring or username to search for.')
            await client.disconnect()
            return
        print(f"Searching for matches for: {SEARCH}")
        matches = await find_all_matches(client, SEARCH)
        if not matches:
            print('No matches found.')
        else:
            print('Matches:')
            for ent, title, eid, ntitle in matches:
                print(f"- id={eid} title='{title}' normalized='{ntitle}'")
        await client.disconnect()

    import asyncio
    asyncio.run(_cli())
