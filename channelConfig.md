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
Asmongold = 20
CDawgVA = 20
GarntM = 20
ConnorDawg = 20
GameLinked = 20
techlinked = 20
TechDweeb = 20
