"""Standalone voice-capture test bot.

Joins one VC, runs the production audio path (``WolfbotAudioSink`` →
``VoiceIngestService``) with STT stubbed out, and writes the same
``audio_debug/`` WAV+txt sidecars Master would. Use it to validate the
recording / PCM-assembly path in isolation, in particular to A/B
``SilenceGeneratorSink`` on/off without running a full game.
"""
