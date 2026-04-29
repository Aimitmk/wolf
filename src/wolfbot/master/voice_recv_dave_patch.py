"""Add DAVE (E2EE voice) inner-decrypt to ``discord-ext-voice-recv``.

discord.py 2.7.1 negotiates DAVE per voice connection and drives the
MLS state machine (``DaveSession``) via the voice gateway opcodes
25-31. discord-ext-voice-recv 0.5.2a179 only handles the *outer* AEAD
layer - when DAVE is active, the AEAD-decrypted bytes are still the
MLS-encrypted opus payload. The opus decoder then sees ciphertext and
fails with ``OpusError("corrupted stream")`` for every packet, which
shows up as a flood of ``voice_recv_opus_decode_skip`` warnings (the
:func:`apply_packet_router_resilience` patch keeps the RX thread
alive but cannot recover the audio).

Discord rolled DAVE out as **mandatory** on many channels, so the
client cannot opt out by advertising ``max_dave_protocol_version=0``
(the voice gateway responds with close code 4017 and won't accept
the connection). The only way to keep recording is to layer the
DAVE decrypt step on top of voice_recv's pipeline ourselves.

The implementation mirrors what receive-side recorders in other
ecosystems do (e.g. Craig on Eris): keep the upstream AEAD outer
decrypt as-is, then call into the active ``DaveSession`` to peel
off the MLS inner layer when the speaker is not in passthrough.

This module monkey-patches :class:`discord.ext.voice_recv.reader.AudioReader`
so that, immediately after the upstream AEAD decrypt returns, an
extra :meth:`davey.DaveSession.decrypt` call replaces the buffer with
the inner-decrypted opus. We use the ``davey`` Python bindings that
discord.py already depends on, and the per-voice-client ``DaveSession``
that discord.py keeps in ``voice_client._connection.dave_session`` —
no extra protocol implementation needed on our side.

Apply via :func:`apply_dave_decrypt_patch` once at startup. Idempotent.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_PATCH_MARKER = "_wolfbot_dave_decrypt_patched"


def apply_dave_decrypt_patch() -> None:
    """Wrap ``AudioReader.decryptor.decrypt_rtp`` to add DAVE inner decrypt.

    Idempotent.  Logs a single info line on first apply so an operator
    can confirm the patch fired before the bot connects to voice.
    """
    try:
        import davey
    except ImportError:
        log.info(
            "voice_recv_dave_patch_skipped reason=davey_not_installed"
        )
        return

    from discord.ext.voice_recv.reader import AudioReader

    if getattr(AudioReader, _PATCH_MARKER, False):
        return

    original_init = AudioReader.__init__

    def patched_init(self, sink, voice_client, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_init(self, sink, voice_client, *args, **kwargs)
        _attach_dave_wrapper(self, voice_client)

    original_update_secret_key = AudioReader.update_secret_key

    def patched_update_secret_key(self, secret_key):  # type: ignore[no-untyped-def]
        # update_secret_key rebuilds the AEAD ``box`` on the upstream
        # decryptor but doesn't rebind ``decrypt_rtp``. Our wrapper
        # closes over the original bound method, so it survives the
        # key rotation - but we still re-attach defensively in case
        # upstream ever rebuilds the bound method too.
        original_update_secret_key(self, secret_key)
        _attach_dave_wrapper(self, self.voice_client)

    AudioReader.__init__ = patched_init  # type: ignore[method-assign]
    AudioReader.update_secret_key = patched_update_secret_key  # type: ignore[method-assign]
    setattr(AudioReader, _PATCH_MARKER, True)
    log.info(
        "voice_recv_dave_decrypt_patch_applied protocol_version=%s",
        davey.DAVE_PROTOCOL_VERSION,
    )


def _attach_dave_wrapper(reader, voice_client) -> None:  # type: ignore[no-untyped-def]
    """Install the DAVE-aware ``decrypt_rtp`` wrapper on the decryptor."""
    import davey

    decryptor = reader.decryptor
    # Don't double-wrap if update_secret_key fires repeatedly during
    # an MLS rotation while a previous wrapper is still in place.
    if getattr(decryptor, _PATCH_MARKER, False):
        return

    original_decrypt_rtp = decryptor.decrypt_rtp

    def dave_aware_decrypt_rtp(packet):  # type: ignore[no-untyped-def]
        data = original_decrypt_rtp(packet)
        state = getattr(voice_client, "_connection", None)
        session = getattr(state, "dave_session", None)
        if session is None or not getattr(session, "ready", False):
            return data

        ssrc = getattr(packet, "ssrc", None)
        if ssrc is None:
            return data
        ssrc_map = getattr(voice_client, "_ssrc_to_id", None) or {}
        user_id = ssrc_map.get(ssrc)
        if user_id is None:
            # First packet for this SSRC — we don't yet know which
            # user it belongs to. Skip DAVE decrypt; the reader will
            # drop unknown-SSRC packets anyway, and once the voice
            # gateway sends the SSRC→user binding, subsequent packets
            # decode normally.
            return data

        try:
            if session.can_passthrough(user_id):
                return data
        except Exception:
            log.debug(
                "dave_can_passthrough_check_failed ssrc=%s user=%s",
                ssrc,
                user_id,
                exc_info=True,
            )
            return data

        try:
            return session.decrypt(user_id, davey.MediaType.audio, data)
        except Exception:
            # Emit at debug level — a flood of decrypt failures during
            # an MLS transition is expected and would otherwise drown
            # the log. The packet_router_resilience patch will
            # downgrade the resulting OpusError to a single line.
            log.debug(
                "dave_decrypt_failed ssrc=%s user=%s",
                ssrc,
                user_id,
                exc_info=True,
            )
            return data

    decryptor.decrypt_rtp = dave_aware_decrypt_rtp
    setattr(decryptor, _PATCH_MARKER, True)


__all__ = ["apply_dave_decrypt_patch"]
