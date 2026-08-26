"""
The Scheduling capability - real doctor calendars, not detected-after-the-fact
commitments.

Two modules split cleanly: availability computes what's free, booking writes
what got taken. Neither knows or cares whether the request came from a voice
call, a WhatsApp thread, or a staff member typing into the dashboard - that
distinction is metadata (Appointment.intake_channel), not a code path.

This is a capability module in the sense the project has settled on: written
once, declared by whichever vertical templates need it (clinic today, salon
later), never subclassed per vertical. See shared/verticals.
"""
