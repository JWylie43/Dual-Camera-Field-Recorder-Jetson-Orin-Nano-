#!/usr/bin/env python3
"""
capture.py - generic GStreamer capture harness for the Orin recorder.

record.py (the SUPERVISOR) spawns this once per stream: once for the video, and
once per audio segment (a new segment each time the mic appears). This process
is deliberately dumb and isolated - it captures one stream and reports one
number. Everything clever (which files exist, hot-plug, the sidecar) lives in
the supervisor. That isolation is the whole point: an audio capture crashing
(mic yanked) can never touch the separate video capture process.

Its three jobs:
  1. Run a pipeline DESCRIPTION (built by record.py) via Gst.parse_launch, so the
     exact, hard-won video pipeline string is reused verbatim - parse_launch is
     what `gst-launch-1.0` itself uses under the hood.
  2. On the FIRST buffer out of the named source element, read that buffer's
     capture time on the pipeline's MONOTONIC clock and print ONE line:
         ANCHOR <monotonic_ns> <utc_iso>
     The supervisor - the SOLE writer of the sidecar - records it. This process
     NEVER writes the sidecar, so two captures can never race on that file.
  3. Stop cleanly on SIGINT by injecting EOS at the source and waiting for it to
     travel through the muxer/filesink - the same clean-EOS contract as
     `gst-launch -e` that gives a finalized, seekable file. On a source ERROR
     (e.g. the mic being unplugged) it makes a best-effort finalize and exits
     non-zero so the supervisor knows the segment ended.

Anchor clock: GstSystemClock defaults to CLOCK_MONOTONIC, and every process on
this Orin reads that same machine-wide monotonic clock. The absolute capture
time of a buffer = pipeline.base_time + buffer.pts (both in monotonic ns). So
the supervisor can line two independently-started streams up exactly by
subtracting their anchors - no shared pipeline, no shared clock object needed,
just the same underlying monotonic timeline.

Usage (invoked by record.py, not by hand):
    python3 capture.py --pipeline "<gst pipeline description>" --probe vsrc
"""

import argparse
import datetime
import signal
import sys

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib  # noqa: E402


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main():
    ap = argparse.ArgumentParser(description="One-stream GStreamer capture with a first-buffer anchor.")
    ap.add_argument("--pipeline", required=True,
                    help="gst-launch-style pipeline description to run.")
    ap.add_argument("--probe", required=True,
                    help="name= of the source element to read the first-buffer anchor from.")
    args = ap.parse_args()

    Gst.init(None)

    try:
        pipeline = Gst.parse_launch(args.pipeline)
    except GLib.Error as e:                       # bad pipeline description
        print(f"ERROR capture: could not parse pipeline: {e}", file=sys.stderr, flush=True)
        sys.exit(2)

    src = pipeline.get_by_name(args.probe)
    if src is None:
        print(f"ERROR capture: no element named '{args.probe}' in the pipeline", file=sys.stderr, flush=True)
        sys.exit(2)

    loop = GLib.MainLoop()
    state = {"anchored": False, "eos_sent": False}

    # --- first-buffer anchor probe on the source's src pad --------------------
    def on_first_buffer(pad, info):
        if state["anchored"]:
            return Gst.PadProbeReturn.REMOVE
        buf = info.get_buffer()
        base = pipeline.get_base_time()
        pts = buf.pts if buf is not None else Gst.CLOCK_TIME_NONE
        if base != Gst.CLOCK_TIME_NONE and pts != Gst.CLOCK_TIME_NONE:
            anchor_ns = base + pts                # the buffer's own capture time
        else:                                     # fallback: read the clock right now
            clk = pipeline.get_clock()
            anchor_ns = clk.get_time() if clk is not None else 0
        state["anchored"] = True
        print(f"ANCHOR {int(anchor_ns)} {_utc_now_iso()}", flush=True)
        return Gst.PadProbeReturn.REMOVE          # fire once, then get out of the data path

    srcpad = src.get_static_pad("src")
    if srcpad is None:
        print(f"ERROR capture: element '{args.probe}' has no static 'src' pad", file=sys.stderr, flush=True)
        sys.exit(2)
    srcpad.add_probe(Gst.PadProbeType.BUFFER, on_first_buffer)

    # --- clean EOS injection --------------------------------------------------
    def send_eos_once():
        if not state["eos_sent"]:
            state["eos_sent"] = True
            # Inject EOS AT THE SOURCE so it flows downstream through the
            # encoder/muxer/filesink and finalizes the file (header, seek index).
            src.send_event(Gst.Event.new_eos())

    # --- bus handling ---------------------------------------------------------
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f"ERROR capture: {err.message} :: {dbg}", file=sys.stderr, flush=True)
            # Best-effort finalize (works if the source is still alive; on a hard
            # unplug the header may stay stale - the desktop merge repairs it).
            send_eos_once()
            GLib.timeout_add(1500, loop.quit)     # don't hang forever waiting for EOS
            state["error"] = True

    bus.connect("message", on_message)

    # --- SIGINT -> clean EOS (integrated with the GLib loop) ------------------
    def on_sigint():
        send_eos_once()
        return True                               # keep the handler installed

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, on_sigint)

    rc = pipeline.set_state(Gst.State.PLAYING)
    if rc == Gst.StateChangeReturn.FAILURE:
        print("ERROR capture: pipeline failed to start (set_state PLAYING failed)", file=sys.stderr, flush=True)
        pipeline.set_state(Gst.State.NULL)
        sys.exit(3)

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)

    sys.exit(1 if state.get("error") else 0)


if __name__ == "__main__":
    main()
