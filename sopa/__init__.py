import importlib.metadata
import logging
import sys
from ._logging import configure_logger

__version__ = importlib.metadata.version("sopa")

log = logging.getLogger("sopa")
configure_logger(log)

if "--help" not in sys.argv:
    from ._settings import settings
    from . import utils
    from . import io
    from . import segmentation

    from .segmentation import tissue_segmentation
    from .patches import make_transcript_patches, make_image_patches
    from ._sdata import get_spatial_image, get_spatial_element, to_intrinsic
