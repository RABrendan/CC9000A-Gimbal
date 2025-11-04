import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib, GObject

from threading import Thread
from time import sleep, time
import sys, tty, termios, select

Gst.init(None)

# ------------------ Config ------------------
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
EO_ZOOM_MAX = 21.0   # lets overlay show up to 20.0x
IR_ZOOM_MIN = 1.0
IR_ZOOM_MAX = 8.0
ZOOM_COOLDOWN = 0.1
# --------------------------------------------

def nv_element_exists(name: str) -> bool:
    return Gst.ElementFactory.find(name) is not None

HAVE_NVCOMPOSITOR = nv_element_exists("nvcompositor")
HAVE_NVJPEGDEC   = nv_element_exists("nvjpegdec")
HAVE_NVVIDCONV   = nv_element_exists("nvvidconv")

# Build the pipeline description depending on available plugins.
# Strategy:
# - Always use nvjpegdec/nvvidconv when available (GPU decode/scale)
# - Prefer nvcompositor (GPU) if present, otherwise fall back to compositor (CPU)
# - Do cropping with videocrop (CPU) for maximum compatibility; zoom math unchanged.
#   (If you want to try full-GPU cropping later, we can switch to nvvidconv src-crop or nvcompositor src crop per pad.)

def build_pipeline_desc():
    # Camera branches (GPU decode path if available)
    eo_src  = f'v4l2src device={EO_DEV} io-mode=2 do-timestamp=true ! image/jpeg,width=1280,height=720,framerate=30/1 ! '
    ir_src  = f'v4l2src device={IR_DEV} io-mode=2 do-timestamp=true ! image/jpeg,width=1280,height=720,framerate=30/1 ! '

    jpegdec = 'nvjpegdec' if HAVE_NVJPEGDEC else 'jpegdec'
    vconv   = 'nvvidconv' if HAVE_NVVIDCONV else 'videoconvert'

    # Convert to NVMM when using nvvidconv; compositor fallback expects system memory.
    # We’ll convert to standard system memory just before compositor if using CPU compositor.
    # If using nvcompositor, we can keep NVMM into it.
    if HAVE_NVCOMPOSITOR:
        comp_name = 'nvcompositor'
        to_full_w  = f'{vconv} ! video/x-raw(memory:NVMM),width={OUT_W},height={OUT_H}'
        to_small_w = f'{vconv} ! video/x-raw(memory:NVMM),width=320,height=180'
        comp_caps  = f'video/x-raw(memory:NVMM),width={OUT_W},height={OUT_H}'
        tail_to_mem = f'{vconv} ! video/x-raw,format=RGBA,width={OUT_W},height={OUT_H}'  # bring to sysmem for textoverlay
    else:
        comp_name = 'compositor'
        to_full_w  = f'{vconv} ! videoscale ! video/x-raw,width={OUT_W},height={OUT_H}'
        to_small_w = f'{vconv} ! videoscale ! video/x-raw,width=320,height=180'
        comp_caps  = f'video/x-raw,width={OUT_W},height={OUT_H}'
        tail_to_mem = ''  # already in sysmem

    desc = f"""
{eo_src}{jpegdec} !
{to_full_w} !
queue max-size-buffers=1 leaky=downstream !
tee name=teo

teo. ! queue max-size-buffers=1 leaky=downstream ! videocrop name=eocrop ! comp.sink_0
teo. ! queue max-size-buffers=1 leaky=downstream ! videocrop name=eocrop_small ! {to_small_w} ! comp.sink_2

{ir_src}{jpegdec} !
{to_full_w} !
queue max-size-buffers=1 leaky=downstream !
tee name=tir

tir. ! queue max-size-buffers=1 leaky=downstream ! videocrop name=ircrop ! comp.sink_1
tir. ! queue max-size-buffers=1 leaky=downstream ! videocrop name=ircrop_small ! {to_small_w} ! comp.sink_3

{comp_name} name=comp background=black !
{comp_caps} !
{tail_to_mem} !
videoconvert !
textoverlay name=overlay valignment=top halignment=center font-desc="Sans 24" !
nvoverlaysink sync=false
"""
    # If you do not have nvoverlaysink on your image, swap the sink to autovideosink:
    # desc = desc.replace("nvoverlaysink", "autovideosink")
    return desc

pipeline_desc = build_pipeline_desc()
pipeline = Gst.parse_launch(pipeline_desc)

# Grab handles
comp = pipeline.get_by_name("comp")
eocrop = pipeline.get_by_name("eocrop")
ircrop = pipeline.get_by_name("ircrop")
eocrop_small = pipeline.get_by_name("eocrop_small")
ircrop_small = pipeline.get_by_name("ircrop_small")
overlay = pipeline.get_by_name("overlay")

# compositor sink pads
pad_cam_full = comp.get_static_pad("sink_0")
pad_ir_full  = comp.get_static_pad("sink_1")
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
    # compositor pad sizes unset
    for p in pads:
        # -1 resets to natural size
        p.set_property("width", -1)
        p.set_property("height", -1)
        p.set_property("alpha", 0.0)

    # reset all crops
    for c in (eocrop, ircrop, eocrop_small, ircrop_small):
        c.set_property("left", 0)
        c.set_property("right", 0)
        c.set_property("top", 0)
        c.set_property("bottom", 0)

def update_overlay_text():
    if current_mode == MODE_WIDE:
        overlay.set_property("text", "WIDE")
    elif current_mode == MODE_EO_ZOOM:
        disp = eo_zoom - 1.0
        overlay.set_property("text", "Zoom %.1fx" % disp)
    elif current_mode == MODE_IR:
        ir_val = derive_ir(eo_zoom)
        overlay.set_property("text", "IR ONLY | %.1fx" % ir_val)
    elif current_mode == MODE_SPLIT:
        disp = eo_zoom - 1.0
        overlay.set_property("text", "SPLIT | Zoom %.1fx" % disp)
    elif current_mode == MODE_PIP_EO:
        disp = eo_zoom - 1.0
        overlay.set_property("text", "PIP (EO BIG) | Zoom %.1fx" % disp)
    elif current_mode == MODE_PIP_IR:
        ir_val = derive_ir(eo_zoom)
        overlay.set_property("text", "PIP (IR BIG) | %.1fx" % ir_val)
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

    eo_w = OUT_W / eo_val
    eo_h = OUT_H / eo_val
    eo_left = int((OUT_W - eo_w) / 2)
    eo_right = OUT_W - int(eo_w) - eo_left
    eo_top = int((OUT_H - eo_h) / 2)
    eo_bottom = OUT_H - int(eo_h) - eo_top

    ir_w = OUT_W / ir_val
    ir_h = OUT_H / ir_val
    ir_left = int((OUT_W - ir_w) / 2)
    ir_right = OUT_W - int(ir_w) - ir_left
    ir_top = int((OUT_H - ir_h) / 2)
    ir_bottom = OUT_H - int(ir_h) - ir_top

    if mode == MODE_EO_ZOOM:
        eocrop.set_property("left", eo_left)
        eocrop.set_property("right", eo_right)
        eocrop.set_property("top", eo_top)
        eocrop.set_property("bottom", eo_bottom)

        pad_cam_full.set_property("alpha", 1.0)
        pad_cam_full.set_property("width", OUT_W)
        pad_cam_full.set_property("height", OUT_H)
        pad_cam_full.set_property("xpos", 0)
        pad_cam_full.set_property("ypos", 0)
        return

    if mode == MODE_IR:
        ircrop.set_property("left", ir_left)
        ircrop.set_property("right", ir_right)
        ircrop.set_property("top", ir_top)
        ircrop.set_property("bottom", ir_bottom)

        pad_ir_full.set_property("alpha", 1.0)
        pad_ir_full.set_property("width", OUT_W)
        pad_ir_full.set_property("height", OUT_H)
        pad_ir_full.set_property("xpos", 0)
        pad_ir_full.set_property("ypos", 0)
        return

    if mode == MODE_SPLIT:
        eo_target_w = 640.0 / eo_val
        extra_eo = eo_w - eo_target_w
        if extra_eo < 0: extra_eo = 0
        extra_eo_each = int(extra_eo / 2)

        eocrop.set_property("left", eo_left + extra_eo_each)
        eocrop.set_property("right", eo_right + extra_eo_each)
        eocrop.set_property("top", eo_top)
        eocrop.set_property("bottom", eo_bottom)
        pad_cam_full.set_property("alpha", 1.0)
        pad_cam_full.set_property("width", 640)
        pad_cam_full.set_property("height", 720)
        pad_cam_full.set_property("xpos", 0)
        pad_cam_full.set_property("ypos", 0)

        ir_target_w = 640.0 / ir_val
        extra_ir = ir_w - ir_target_w
        if extra_ir < 0: extra_ir = 0
        extra_ir_each = int(extra_ir / 2)

        ircrop.set_property("left", ir_left + extra_ir_each)
        ircrop.set_property("right", ir_right + extra_ir_each)
        ircrop.set_property("top", ir_top)
        ircrop.set_property("bottom", ir_bottom)
        pad_ir_full.set_property("alpha", 1.0)
        pad_ir_full.set_property("width", 640)
        pad_ir_full.set_property("height", 720)
        pad_ir_full.set_property("xpos", 640)
        pad_ir_full.set_property("ypos", 0)
        return

    if mode == MODE_PIP_EO:
        eocrop.set_property("left", eo_left)
        eocrop.set_property("right", eo_right)
        eocrop.set_property("top", eo_top)
        eocrop.set_property("bottom", eo_bottom)
        pad_cam_full.set_property("alpha", 1.0)
        pad_cam_full.set_property("width", OUT_W)
        pad_cam_full.set_property("height", OUT_H)
        pad_cam_full.set_property("xpos", 0)
        pad_cam_full.set_property("ypos", 0)

        ircrop_small.set_property("left", ir_left)
        ircrop_small.set_property("right", ir_right)
        ircrop_small.set_property("top", ir_top)
        ircrop_small.set_property("bottom", ir_bottom)
        pad_ir_small.set_property("alpha", 1.0)
        pad_ir_small.set_property("width", 320)
        pad_ir_small.set_property("height", 180)
        pad_ir_small.set_property("xpos", OUT_W - 320)
        pad_ir_small.set_property("ypos", OUT_H - 180)
        pad_ir_small.set_property("zorder", 10)
        return

    if mode == MODE_PIP_IR:
        ircrop.set_property("left", ir_left)
        ircrop.set_property("right", ir_right)
        ircrop.set_property("top", ir_top)
        ircrop.set_property("bottom", ir_bottom)
        pad_ir_full.set_property("alpha", 1.0)
        pad_ir_full.set_property("width", OUT_W)
        pad_ir_full.set_property("height", OUT_H)
        pad_ir_full.set_property("xpos", 0)
        pad_ir_full.set_property("ypos", 0)

        eocrop_small.set_property("left", eo_left)
        eocrop_small.set_property("right", eo_right)
        eocrop_small.set_property("top", eo_top)
        eocrop_small.set_property("bottom", eo_bottom)
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
        r, _, _ = select.select([sys.stdin], [], [], 0) if self.is_tty else ([], [], [])
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

# Main loop
main_loop = GLib.MainLoop()
main_loop_thread = Thread(target=main_loop.run, daemon=True)
main_loop_thread.start()

pipeline.set_state(Gst.State.PLAYING)
# default to WIDE
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
