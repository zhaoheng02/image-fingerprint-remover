"""Known AI-generation and editor fingerprint signatures."""
import re

# PNG text-chunk keys that strongly indicate AI provenance.
# Mapping: lowercase key -> (label, description)
PNG_AI_TEXT_KEYS = {
    "parameters": ("Stable Diffusion (A1111)",
                   "Automatic1111 web-UI stores full prompt, seed, model hash, sampler in this key."),
    "prompt": ("ComfyUI / generic prompt",
               "ComfyUI stores the API prompt JSON in this key."),
    "workflow": ("ComfyUI workflow",
                 "ComfyUI stores the full node graph JSON in this key."),
    "comfy": ("ComfyUI",
              "Marker key written by ComfyUI exporters."),
    "sd-metadata": ("InvokeAI",
                    "InvokeAI stores its generation metadata under this key."),
    "invokeai": ("InvokeAI", "InvokeAI generation marker."),
    "novelai": ("NovelAI", "NovelAI generation marker."),
    "dream": ("Dream/InvokeAI", "InvokeAI dream parameters key."),
    "openai": ("OpenAI / ChatGPT", "OpenAI image-generation marker."),
    "dall-e": ("DALL-E", "DALL-E generation marker."),
    "midjourney": ("Midjourney", "Midjourney generation marker."),
    "firefly": ("Adobe Firefly", "Adobe Firefly generation marker."),
}

# Substrings that, if found inside text-chunk values, flag AI origin.
PNG_AI_VALUE_PATTERNS = [
    (re.compile(r"\bSteps:\s*\d+", re.I), "Stable Diffusion A1111 parameter block"),
    (re.compile(r"\bSampler:\s*[A-Za-z]", re.I), "Stable Diffusion sampler block"),
    (re.compile(r"\bModel hash:\s*[0-9a-f]{6,}", re.I), "Stable Diffusion model hash"),
    (re.compile(r"\bCFG scale:\s*[\d.]+", re.I), "Stable Diffusion CFG scale"),
    (re.compile(r'"class_type"\s*:', re.I), "ComfyUI node graph JSON"),
    (re.compile(r"midjourney|--ar\s+\d+:\d+|--v\s+\d", re.I), "Midjourney prompt block"),
    (re.compile(r"dall[\s\-]?e|gpt[\s\-]?image|openai", re.I), "OpenAI/DALL-E marker"),
    (re.compile(r"stable[\s\-]?diffusion|automatic1111|a1111", re.I), "Stable Diffusion marker"),
    (re.compile(r"firefly|adobe stock", re.I), "Adobe marker"),
    (re.compile(r"trainedalgorithmicmedia|compositewithtrainedalgorithmicmedia", re.I),
     "IPTC DigitalSourceType = AI-generated"),
]

# XMP / IPTC tags that flag AI origin.
XMP_AI_PATTERNS = [
    (re.compile(r"Iptc4xmpExt:DigitalSourceType[^<]*trainedAlgorithmicMedia", re.I),
     "IPTC DigitalSourceType = trainedAlgorithmicMedia"),
    (re.compile(r"Iptc4xmpExt:DigitalSourceType[^<]*compositeWithTrainedAlgorithmicMedia", re.I),
     "IPTC DigitalSourceType = compositeWithTrainedAlgorithmicMedia"),
    (re.compile(r"<xmpMM:History>.*?</xmpMM:History>", re.S | re.I),
     "Adobe XMP edit history (may leak editor + actions)"),
    (re.compile(r"<photoshop:[A-Z]\w+>", re.I), "Photoshop XMP tag"),
    (re.compile(r"firefly|adobe stock|adobestock", re.I), "Adobe Firefly/Stock marker"),
    (re.compile(r"openai|chatgpt|dall[\s\-]?e", re.I), "OpenAI/DALL-E marker"),
    (re.compile(r"midjourney", re.I), "Midjourney marker"),
    (re.compile(r"stable[\s\-]?diffusion|stability\.ai", re.I), "Stable Diffusion marker"),
    (re.compile(r"generativeAI|generative-ai", re.I), "Generic generative-AI marker"),
]

# JUMBF / C2PA box magic — both PNG private chunk and JPEG APP11 carry this.
JUMBF_MAGIC = b"jumb"
C2PA_LABELS = [b"c2pa", b"c2ma", b"c2as", b"c2cl"]

# Photoshop IRB marker.
PHOTOSHOP_8BIM = b"8BIM"
PHOTOSHOP_HEADER = b"Photoshop 3.0\x00"

# Adobe APP14 marker.
ADOBE_APP14 = b"Adobe\x00"

# EXIF tags that always leak.
EXIF_LEAK_TAGS = {
    "Make", "Model", "Software", "BodySerialNumber", "SerialNumber",
    "LensSerialNumber", "OwnerName", "Artist", "Copyright", "UserComment",
    "ImageUniqueID", "DateTimeOriginal", "DateTimeDigitized", "ImageDescription",
    "HostComputer", "CameraOwnerName",
}

GPS_TAGS_PREFIX = "GPS"

# Standard PNG chunks we keep by default.
PNG_CRITICAL_CHUNKS = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
# Ancillary chunks we keep in "safe" mode (purely rendering-related, no identifier).
PNG_SAFE_ANCILLARY = {
    b"tRNS",  # transparency
    b"gAMA",  # gamma
    b"cHRM",  # chromaticities
    b"sBIT",  # significant bits
    b"bKGD",  # background color
    b"hIST",  # histogram
    b"sRGB",  # color space declaration (1 byte rendering intent)
    b"acTL", b"fcTL", b"fdAT",  # APNG animation
}
