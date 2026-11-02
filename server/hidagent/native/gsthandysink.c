/* Timestamped transport for the ESP32 iPhone Agent. MIT; see repository LICENSE.
 * Keep buffer PTS across the process boundary, including independent pipeline
 * base times. There is deliberately no wall-clock pacing or late-buffer drop.
 */
#include <gst/base/gstbasesink.h>
#include <errno.h>
#include <poll.h>
#include <unistd.h>

typedef struct {
    GstBaseSink parent;
    gint fd, flushing;
    guint kind, rate, channels, sequence;
    gint64 clock_offset;
    gboolean have_clock;
} HandySink;
typedef struct { GstBaseSinkClass parent; } HandySinkClass;
G_DEFINE_TYPE(HandySink, handy_sink, GST_TYPE_BASE_SINK)

static GstStaticPadTemplate pad = GST_STATIC_PAD_TEMPLATE("sink", GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS("image/jpeg; audio/x-raw, format=S16LE, layout=interleaved, "
                    "rate=[1,384000], channels=[1,8]"));

static gboolean write_all(HandySink *s, const guint8 *p, gsize n) {
    while (n && !g_atomic_int_get(&s->flushing)) {
        struct pollfd fd = {s->fd, POLLOUT, 0};
        int ready = poll(&fd, 1, 100);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0 || (ready > 0 && !(fd.revents & POLLOUT))) return FALSE;
        if (!ready) continue;
        /* POSIX guarantees PIPE_BUF >= 512 (macOS uses 512). Keep writes
         * atomic so a writable pipe cannot block on a large JPEG. */
        ssize_t done = write(s->fd, p, MIN(n, 512));
        if (done < 0 && errno == EINTR) continue;
        if (done <= 0) return FALSE;
        n -= done; p += done;
    }
    return n == 0;
}

static gboolean start(GstBaseSink *base) {
    HandySink *s = (HandySink *)base;
    s->have_clock = FALSE; s->sequence = 0;
    g_atomic_int_set(&s->flushing, FALSE);
    /* UxPlay enables timestamp assignment with -vsync / -async. It also sets
     * sink sync=true; override pacing here while retaining every buffer's PTS. */
    gst_base_sink_set_sync(base, FALSE);
    gst_base_sink_set_async_enabled(base, FALSE);
    gst_base_sink_set_qos_enabled(base, FALSE);
    gst_base_sink_set_max_lateness(base, -1);
    return s->fd >= 0;
}

static gboolean unlock(GstBaseSink *base) {
    g_atomic_int_set(&((HandySink *)base)->flushing, TRUE); return TRUE;
}
static gboolean unlock_stop(GstBaseSink *base) {
    g_atomic_int_set(&((HandySink *)base)->flushing, FALSE); return TRUE;
}

static gboolean set_caps(GstBaseSink *base, GstCaps *caps) {
    HandySink *s = (HandySink *)base;
    const GstStructure *c = gst_caps_get_structure(caps, 0);
    s->rate = s->channels = 0;
    if (gst_structure_has_name(c, "image/jpeg")) { s->kind = 2; return TRUE; }
    gint rate, channels;
    if (!gst_structure_has_name(c, "audio/x-raw") ||
        !gst_structure_get_int(c, "rate", &rate) || !gst_structure_get_int(c, "channels", &channels) ||
        g_strcmp0(gst_structure_get_string(c, "format"), "S16LE")) return FALSE;
    s->kind = 1; s->rate = rate; s->channels = channels;
    return TRUE;
}

static GstFlowReturn render(GstBaseSink *base, GstBuffer *buffer) {
    HandySink *s = (HandySink *)base;
    guint64 pts = GST_BUFFER_PTS(buffer), running = GST_CLOCK_TIME_NONE, timestamp = 0;
    guint flags = GST_BUFFER_FLAG_IS_SET(buffer, GST_BUFFER_FLAG_DISCONT) ? 2 : 0;
    GstClock *clock = gst_element_get_clock(GST_ELEMENT(base));
    if (clock && GST_CLOCK_TIME_IS_VALID(pts)) {
        running = gst_segment_to_running_time(&base->segment, GST_FORMAT_TIME, pts);
        if (GST_CLOCK_TIME_IS_VALID(running)) {
            if (!s->have_clock) {
                /* UxPlay uses a REALTIME GstSystemClock. Preserve it exactly.
                 * For other clocks (tests), establish a fixed Unix mapping. */
                GstClockType type = GST_CLOCK_TYPE_MONOTONIC;
                if (GST_IS_SYSTEM_CLOCK(clock)) g_object_get(clock, "clock-type", &type, NULL);
                s->clock_offset = type == GST_CLOCK_TYPE_REALTIME ? 0 :
                    (gint64)g_get_real_time() * 1000 - (gint64)gst_clock_get_time(clock);
                s->have_clock = TRUE;
            }
            timestamp = gst_element_get_base_time(GST_ELEMENT(base)) + running + s->clock_offset;
            flags |= 1;
        }
    }
    if (clock) gst_object_unref(clock);
    GstMapInfo map;
    if (!gst_buffer_map(buffer, &map, GST_MAP_READ)) return GST_FLOW_ERROR;
    if (map.size > 16 * 1024 * 1024) { gst_buffer_unmap(buffer, &map); return GST_FLOW_ERROR; }
    /* Network order; see media_transport.HEADER. Missing PTS is explicit, never guessed. */
    guint8 header[40] = {'H','C','A','P',1,0,0,0};
    header[5] = s->kind;
    GST_WRITE_UINT16_BE(header + 6, flags);
    GST_WRITE_UINT64_BE(header + 8, timestamp);
    GST_WRITE_UINT64_BE(header + 16, GST_BUFFER_DURATION_IS_VALID(buffer) ? GST_BUFFER_DURATION(buffer) : 0);
    GST_WRITE_UINT32_BE(header + 24, map.size);
    GST_WRITE_UINT32_BE(header + 28, s->rate);
    GST_WRITE_UINT32_BE(header + 32, s->channels);
    GST_WRITE_UINT32_BE(header + 36, s->sequence++);
    gboolean ok = write_all(s, header, sizeof(header)) && write_all(s, map.data, map.size);
    gst_buffer_unmap(buffer, &map);
    return ok ? GST_FLOW_OK : (g_atomic_int_get(&s->flushing) ? GST_FLOW_FLUSHING : GST_FLOW_ERROR);
}

static void set_property(GObject *object, guint id, const GValue *value, GParamSpec *spec) {
    if (id == 1) ((HandySink *)object)->fd = g_value_get_int(value);
    else G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, spec);
}
static void get_property(GObject *object, guint id, GValue *value, GParamSpec *spec) {
    if (id == 1) g_value_set_int(value, ((HandySink *)object)->fd);
    else G_OBJECT_WARN_INVALID_PROPERTY_ID(object, id, spec);
}
static void handy_sink_class_init(HandySinkClass *klass) {
    GObjectClass *object = G_OBJECT_CLASS(klass);
    object->set_property = set_property; object->get_property = get_property;
    g_object_class_install_property(object, 1, g_param_spec_int("fd", "fd", "Inherited output fd",
        -1, G_MAXINT, -1, G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS));
    GstElementClass *element = GST_ELEMENT_CLASS(klass);
    gst_element_class_add_static_pad_template(element, &pad);
    gst_element_class_set_static_metadata(element, "Timestamped capture", "Sink",
        "Preserve decoded media and its clock across an inherited pipe", "ESP32 iPhone Agent contributors");
    GstBaseSinkClass *base = GST_BASE_SINK_CLASS(klass);
    base->start = start; base->unlock = unlock; base->unlock_stop = unlock_stop;
    base->set_caps = set_caps; base->render = render;
}
static void handy_sink_init(HandySink *s) { s->fd = -1; }
static gboolean plugin_init(GstPlugin *plugin) {
    return gst_element_register(plugin, "handysink", GST_RANK_NONE, handy_sink_get_type());
}
#define PACKAGE "hidagent"
GST_PLUGIN_DEFINE(GST_VERSION_MAJOR, GST_VERSION_MINOR, handysink,
    "Timestamped local media transport", plugin_init, "1.0", "MIT/X11", PACKAGE,
    "https://github.com/onehumanbeing/esp32-iphone-agent")
