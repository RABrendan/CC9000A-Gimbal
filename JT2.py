# jetson_view_gpu_opt.py (plain ASCII)

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

from threading import Thread
from time import sleep, time
import sys, tty, termios, select

Gst.init(None)

# ---------- Config ----------
EO_DEV = "/dev/video0"
IR_DEV = "/dev/video2"
OUT_W = 1280
OUT_H = 720

MODE_WIDE = 0
MODE_EO_ZOOM = 1
MODE_IR = 2
MODE_SPLIT = 3
MODE_PIP_EO = 4
MODE_PIP_IR = 5
NUM_MODES = 6

EO_ZOOM_MIN = 2.0
EO_ZOOM_MAX = 21.0   # displays up to 20.0x (we show eo_zoom - 1)
IR_ZOOM_MIN = 1.0
IR_ZOOM_MAX = 8.0
ZOOM_COOLDOWN = 0.1
# ----------------------------

def have(name):
    return Gst.ElementFactory.find(name) is not None

HAVE_NVJPEGDEC = have("nvjpegdec")
HAVE_NVVIDCONV = have("nvvidconv")
HAVE_NVCOMPOSITOR = have("nvcompositor")

def choose_sink():
    for s in ("glimagesink", "xvimagesink", "autovideosink"):
        if have(s):
            return s
    return "fakesink"

# Build pipeline. We will use GPU path if nv* present, else CPU fallback.
def build_pipeline_desc():
    sink = choose_sink()
    jpegdec = "nvjpegdec" if HAVE_NVJPEGDEC else "jpegdec"
    vconv   = "nvvidconv" if HAVE_NVVIDCONV else "videoconvert"

    # Full-size branch converter/caps
    if HAVE_NVVIDCONV:
        to_full  = f"{vconv} ! video/x-raw(memory:NVMM),format=NV12,width={OUT_W},height={OUT_H}"
        to_small = f"{vconv} ! video/x-raw(memory:NVMM),format=NV12,width=320,height=180"
        comp_caps = f"video/x-raw(memory:NVMM),format=NV12,width={OUT_W},height={OUT_H}"
        # textoverlay needs sysmem; convert after compositor
        to_sysmem_after_comp = f"{vconv} ! video/x-raw,format=RGBA,width={OUT_W},height={OUT_H}"
    else:
        to_full  = f"{vconv} ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H}"
        to_small = f"{vconv} ! videoscale ! video/x-raw,width=320,height=180"
        comp_caps = f"video/x-raw,width={OUT_W},height={OUT_H}"
        to_sysmem_after_comp = ""

    comp_name = "nvcompositor" if HAVE_NVCOMPOSITOR else "compositor"

    desc = f"""
v4l2src device={EO_DEV} io-mode=2 do-timestamp=true !
image/jpeg,width=1280,height=720,framerate=30/1 ! {jpegdec} !
queue max-size-buffers=1 leaky=downstream !
{to_full} ! queue max-size-buffers=1 leaky=downstream ! tee name=teo

# EO full (crop on GPU if nvvidconv is present)
teo. ! queue max-size-buffers=1 leaky=downstream ! {('nvvidconv name=eocrop' if HAVE_NVVIDCONV else 'videocrop name=eocrop')} ! comp.sink_0

# EO small PIP source
teo. ! queue max-size-buffers=1 leaky=downstream ! {('nvvidconv name=eocrop_small' if HAVE_NVVIDCONV else 'videocrop name=eocrop_small')} ! {to_small} ! comp.sink_2

v4l2src device={IR_DEV} io-mode=2 do-timestamp=true !
image/jpeg,width=1280,height=720,framerate=30/1 ! {jpegdec} !
queue max-size-buffers=1 leaky=downstream !
{to_full} ! queue max-size-buffers=1 leaky=downstream ! tee name=tir

# IR full
tir. ! queue max-size-buffers=1 leaky=downstream ! {('nvvidconv name=ircrop' if HAVE_NVVIDCONV else 'videocrop name=ircrop')} ! comp.sink_1

# IR small PIP source
tir. ! queue max-size-buffers=1 leaky=downstream ! {('nvvidconv name=ircrop_small' if HAVE_NVVIDCONV else 'videocrop name=ircrop_small')} ! {to_small} ! comp.sink_3

{comp_name} name=comp background=black !
{comp_caps} !
{to_sysmem_after_comp} !
videoconvert !
textoverlay name=overlay valignment=top halignment=center font-desc="Sans 24" !
{sink} name=outsink
"""
    return desc

pipeline_desc = build_pipeline_desc()
pipeline = Gst.parse_launch(pipeline_desc)

# Elements
comp = pipeline.get_by_name("comp")
overlay = pipeline.get_by_name("overlay")
outsink = pipeline.get_by_name("outsink")
try:
    outsink.set_property("sync", False)
except Exception:
    pass

# Crop elements (may be nvvidconv or videocrop depending on availability)
eocrop = pipeline.get_by_name("eocrop")
ircrop = pipeline.get_by_name("ircrop")
eocrop_small = pipeline.get_by_name("eocrop_small")
ircrop_small = pipeline.get_by_name("ircrop_small")

# compositor sink pads
pad_cam_full  = comp.get_static_pad("sink_0")
pad_ir_full   = comp.get_static_pad("sink_1")
pad_cam_small = comp.get_static_pad("sink_2")
pad_ir_small  = comp.get_static_pad("sink_3")
pads = [pad_cam_full, pad_ir_full, pad_cam_small, pad_ir_small]

# Zoom state
eo_zoom = 2.0
current_mode = MODE_WIDE
last_zoom_time = 0.0

def clamp_eo(val):
    if val < EO_ZOOM_MIN: val = EO_ZOOM_MIN
    if val > EO_ZOOM_MAX: val = EO_ZOOM_MAX
    return val

def derive_ir(eo_val):
    ir_val = eo_val - 1.0
    if ir_val < IR_ZOOM_MIN: ir_val = IR_ZOOM_MIN
    if ir_val > IR_ZOOM_MAX: ir_val = IR_ZOOM_MAX
    return ir_val

def eo_step_up(val):
    if val < 4.0: return 0.1
    elif val < 10.0: return 1.0
    elif 10.0 <= val < 11.0: return 1.0
    else: return 2.5

def eo_step_down_clean(val):
    if val > 10.0:
        new_val = val - 2.5
        if new_val < 10.0: new_val = 10.0
        return new_val
    elif val > 4.0:
        new_val = val - 1.0
        if new_val < 4.0: new_val = 4.0
        return new_val
    else:
        new_val = round(val - 0.1, 1)
        if new_val < 2.0: new_val = 2.0
        return new_val

def reset_pads_and_crops():
    for p in pads:
        p.set_property("width", -1)
        p.set_property("height", -1)
        p.set_property("alpha", 0.0)
    # reset crop on both GPU and CPU crop elements
    for c in (eocrop, ircrop, eocrop_small, ircrop_small):
        if c is None: continue
        if HAVE_NVVIDCONV and c.get_factory().get_name() == "nvvidconv":
            # nvvidconv uses src-crop "x,y,w,h"; empty string disables crop
            try:
                c.set_property("src-crop", "")
            except Exception:
                pass
        else:
            # videocrop
            for prop in ("left","right","top","bottom"):
                try:
                    c.set_property(prop, 0)
                except Exception:
                    pass

def set_nv_crop(elem, left, top, width, height):
    # Try GPU crop first
    if HAVE_NVVIDCONV and elem and elem.get_factory().get_name() == "nvvidconv":
        try:
            elem.set_property("src-crop", "%d,%d,%d,%d" % (left, top, width, height))
            return True
        except Exception:
            return False
    return False

def set_cpu_crop(elem, left, right, top, bottom):
    if elem is None: return
    try: elem.set_property("left", left)
    except Exception: pass
    try: elem.set_property("right", right)
    except Exception: pass
    try: elem.set_property("top", top)
    except Exception: pass
    try: elem.set_property("bottom", bottom)
    except Exception: pass

def update_overlay_text():
    if current_mode == MODE_WIDE:
        overlay.set_property("text", "WIDE")
    elif current_mode == MODE_EO_ZOOM:
        disp = eo_zoom - 1.0
        overlay.set_property("text", "Zoom %.1fx" % disp)
    elif current_mode == MODE_IR:
        overlay.set_property("text", "IR ONLY | %.1fx" % derive_ir(eo_zoom))
    elif current_mode == MODE_SPLIT:
        overlay.set_property("text", "SPLIT | Zoom %.1fx" % (eo_zoom - 1.0))
    elif current_mode == MODE_PIP_EO:
        overlay.set_property("text", "PIP (EO BIG) | Zoom %.1fx" % (eo_zoom - 1.0))
    elif current_mode == MODE_PIP_IR:
        overlay.set_property("text", "PIP (IR BIG) | %.1fx" % derive_ir(eo_zoom))
    else:
        overlay.set_property("text", "")

def apply_zoom(mode):
    reset_pads_and_crops()

    if mode == MODE_WIDE:
        pad_cam_full.set_property("alpha", 1.0)
        pad_cam_full.set_property("width", OUT_W)
        pad_cam_full.set_property("height", OUT_H)
        pad_cam_full.set_property("xpos", 0)
        pad_cam_full.set_property("ypos", 0)
        return

    eo_val = eo_zoom
    ir_val = derive_ir(eo_val)

    # compute centered crop boxes at output scale
    eo_w = int(OUT_W / eo_val); eo_h = int(OUT_H / eo_val)
    eo_left = (OUT_W - eo_w) // 2; eo_top = (OUT_H - eo_h) // 2
    eo_right = OUT_W - eo_w - eo_left; eo_bottom = OUT_H - eo_h - eo_top

    ir_w = int(OUT_W / ir_val); ir_h = int(OUT_H / ir_val)
    ir_left = (OUT_W - ir_w) // 2; ir_top = (OUT_H - ir_h) // 2
    ir_right = OUT_W - ir_w - ir_left; ir_bottom = OUT_H - ir_h - ir_top

    if mode == MODE_EO_ZOOM:
        # full EO
        if not set_nv_crop(eocrop, eo_left, eo_top, eo_w, eo_h):
            set_cpu_crop(eocrop, eo_left, eo_right, eo_top, eo_bottom)
        pad_cam_full.set_property("alpha", 1.0)
        pad_cam_full.set_property("width", OUT_W)
        pad_cam_full.set_property("height", OUT_H)
        pad_cam_full.set_property("xpos", 0)
        pad_cam_full.set_property("ypos", 0)
        return

    if mode == MODE_IR:
        if not set_nv_crop(ircrop, ir_left, ir_top, ir_w, ir_h):
            set_cpu_crop(ircrop, ir_left, ir_right, ir_top, ir_bottom)
        pad_ir_full.set_property("alpha", 1.0)
        pad_ir_full.set_property("width", OUT_W)
        pad_ir_full.set_property("height", OUT_H)
        pad_ir_full.set_property("xpos", 0)
        pad_ir_full.set_property("ypos", 0)
        return

    if mode == MODE_SPLIT:
        # EO half
        eo_target_w = 640 // eo_val
        extra_eo = max(0, (OUT_W // eo_val) - eo_target_w)
        extra_eo_each = extra_eo // 2
        if not set_nv_crop(eocrop, eo_left + extra_eo_each, eo_top, eo_w - 2*extra_eo_each, eo_h):
            set_cpu_crop(eocrop, eo_left + extra_eo_each, eo_right + extra_eo_each, eo_top, eo_bottom)
        pad_cam_full.set_property("alpha", 1.0)
        pad_cam_full.set_property("width", 640)
        pad_cam_full.set_property("height", 720)
        pad_cam_full.set_property("xpos", 0)
        pad_cam_full.set_property("ypos", 0)

        # IR half
        ir_target_w = 640 // ir_val
        extra_ir = max(0, (OUT_W // ir_val) - ir_target_w)
        extra_ir_each = extra_ir // 2
        if not set_nv_crop(ircrop, ir_left + extra_ir_each, ir_top, ir_w - 2*extra_ir_each, ir_h):
            set_cpu_crop(ircrop, ir_left + extra_ir_each, ir_right + extra_ir_each, ir_top, ir_bottom)
        pad_ir_full.set_property("alpha", 1.0)
        pad_ir_full.set_property("width", 640)
        pad_ir_full.set_property("height", 720)
        pad_ir_full.set_property("xpos", 640)
        pad_ir_full.set_property("ypos", 0)
        return

    if mode == MODE_PIP_EO:
        if not set_nv_crop(eocrop, eo_left, eo_top, eo_w, eo_h):
            set_cpu_crop(eocrop, eo_left, eo_right, eo_top, eo_bottom)
        pad_cam_full.set_property("alpha", 1.0)
        pad_cam_full.set_property("width", OUT_W)
        pad_cam_full.set_property("height", OUT_H)
        pad_cam_full.set_property("xpos", 0)
        pad_cam_full.set_property("ypos", 0)

        if not set_nv_crop(ircrop_small, ir_left, ir_top, ir_w, ir_h):
            set_cpu_crop(ircrop_small, ir_left, ir_right, ir_top, ir_bottom)
        pad_ir_small.set_property("alpha", 1.0)
        pad_ir_small.set_property("width", 320)
        pad_ir_small.set_property("height", 180)
        pad_ir_small.set_property("xpos", OUT_W - 320)
        pad_ir_small.set_property("ypos", OUT_H - 180)
        pad_ir_small.set_property("zorder", 10)
        return

    if mode == MODE_PIP_IR:
        if not set_nv_crop(ircrop, ir_left, ir_top, ir_w, ir_h):
            set_cpu_crop(ircrop, ir_left, ir_right, ir_top, ir_bottom)
        pad_ir_full.set_property("alpha", 1.0)
        pad_ir_full.set_property("width", OUT_W)
        pad_ir_full.set_property("height", OUT_H)
        pad_ir_full.set_property("xpos", 0)
        pad_ir_full.set_property("ypos", 0)

        if not set_nv_crop(eocrop_small, eo_left, eo_top, eo_w, eo_h):
            set_cpu_crop(eocrop_small, eo_left, eo_right, eo_top, eo_bottom)
        pad_cam_small.set_property("alpha", 1.0)
        pad_cam_small.set_property("width", 320)
        pad_cam_small.set_property("height", 180)
        pad_cam_small.set_property("xpos", OUT_W - 320)
        pad_cam_small.set_property("ypos", OUT_H - 180)
        pad_cam_small.set_property("zorder", 10)
        return

def set_mode(mode):
    for p in pads:
        p.set_property("alpha", 0.0)
    global current_mode
    current_mode = mode
    apply_zoom(mode)
    update_overlay_text()

def schedule_apply():
    def _do():
        apply_zoom(current_mode)
        update_overlay_text()
        return False
    GLib.idle_add(_do)

# Keyboard handling (SPACE / UP/i / DOWN/k)
class KB:
    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.is_tty = sys.stdin.isatty()
        if self.is_tty:
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        else:
            self.old = None
    def restore(self):
        if self.is_tty and self.old:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
    def read_key(self):
        r,_,_ = select.select([sys.stdin], [], [], 0) if self.is_tty else ([],[],[])
        if not r: return None
        ch1 = sys.stdin.read(1)
        if ch1 == " ": return b"SPACE"
        if ch1 in ("i","I"): return b"UP"
        if ch1 in ("k","K"): return b"DOWN"
        if ch1 == "\x1b":
            r,_,_ = select.select([sys.stdin], [], [], 0.002)
            if not r: return None
            ch2 = sys.stdin.read(1)
            if ch2 != "[": return None
            r,_,_ = select.select([sys.stdin], [], [], 0.002)
            if not r: return None
            ch3 = sys.stdin.read(1)
            if ch3 == "A": return b"UP"
            if ch3 == "B": return b"DOWN"
        return None

# Main
main_loop = GLib.MainLoop()
main_loop_thread = Thread(target=main_loop.run, daemon=True)
main_loop_thread.start()

pipeline.set_state(Gst.State.PLAYING)
eo_zoom = 2.0
set_mode(MODE_WIDE)

if not sys.stdin.isatty():
    print("Note: stdin not a TTY. Use i/k for zoom, SPACE to switch modes.")
print("Controls: SPACE=next | UP/i=zoom in | DOWN/k=zoom out | Ctrl+C quits")

last_zoom_time = time()
kb = KB()

try:
    while True:
        now = time()
        key = kb.read_key()

        if key == b"UP":
            if current_mode != MODE_WIDE and (now - last_zoom_time) >= ZOOM_COOLDOWN:
                step = eo_step_up(eo_zoom)
                eo_zoom = clamp_eo(round(eo_zoom + step, 2))
                schedule_apply()
                last_zoom_time = now

        elif key == b"DOWN":
            if current_mode != MODE_WIDE and (now - last_zoom_time) >= ZOOM_COOLDOWN:
                eo_zoom = eo_step_down_clean(eo_zoom)
                eo_zoom = clamp_eo(eo_zoom)
                schedule_apply()
                last_zoom_time = now

        elif key == b"SPACE":
            next_mode = (current_mode + 1) % NUM_MODES
            def _sw():
                set_mode(next_mode)
                return False
            GLib.idle_add(_sw)

        sleep(0.03)

except KeyboardInterrupt:
    pass
finally:
    kb.restore()

pipeline.set_state(Gst.State.NULL)
main_loop.quit()
main_loop_thread.join()
print("Stopped.")
