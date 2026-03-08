# shim: re-export from open-agents via _oa_loader
import sys
from utils._oa_loader import oa_utils as _oa  # noqa: triggers loader

_ch = sys.modules["_oa_utils._channel"]
create_channel = _ch.create_channel
send_to_channel = _ch.send_to_channel
close_channel = _ch.close_channel
receive_from_channel = _ch.receive_from_channel
ChannelClosedError = _ch.ChannelClosedError
channel_exists = _ch.channel_exists
list_channels = _ch.list_channels
