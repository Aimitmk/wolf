"""NPC bot worker package.

Code that runs only inside an NPC bot worker process (one process per NPC):
the WS client, the Grok-driven speech generator, the VOICEVOX TTS adapter,
and the discord.py voice playback wrapper.

Master-side counterparts (NPC registry, speak arbiter, ingest, etc.) live
in :mod:`wolfbot.master`.
"""
