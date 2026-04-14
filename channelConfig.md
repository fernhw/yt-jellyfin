# Channel Config
# Used by downloadSubs.sh for priority ordering and scan limits

# PRIORITY - premium channels, always downloaded first (in order)
# These get processed before anything else in the queue
[priority]
Max0r
videogamedunkey
TrashTaste
SummoningSalt
InternetHistorian
supereyepatchwolf0
Fireship
GMTK
CaptainDisillusion
KBash

# LIMITS - max videos kept on disk per channel
# When a new video downloads and count exceeds limit,
# oldest videos are deleted (bottom out, top in)
# Channels not listed here: unlimited (keep all)
# Format: handle = number
[limits]
Asmongold = 10
CDawgVA = 10
GarntM = 10
ConnorDawg = 10
