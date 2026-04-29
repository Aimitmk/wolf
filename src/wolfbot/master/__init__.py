"""Master-side reactive-voice pipeline.

Code that runs only inside the Master Discord bot process when the
``reactive_voice`` discussion mode is active: the WS server, NPC registry,
speak arbiter, voice-ingest VAD/STT, and audio sink. NPC-bot-side
counterparts live in :mod:`wolfbot.npc`.
"""
