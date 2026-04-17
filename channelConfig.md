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
hbomberguy
Matthewmatosis
NakeyJakey
ActionButton
SsethTzeentach
RedLetterMedia
MichaelReeves
Nerdwriter1
GLITCH
Nerrel
Echidnut
SchaffrillasProductions
IHincognitoMode
StudioWrong
AlphaBetaGamer

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

# QUALITY - max resolution per channel
# Channels not listed here: best available (default)
# Format: handle = max_height (e.g. 1080, 720, 480)
[quality]
KBash = 1080
Yessenia = 1080
ConnorDawg = 1080
DigitalFoundry = 1080
TrashTaste = 1080
NeverKnowsBest = 1080

# PODCASTABLE - channels whose videos work well as audio-only
# Audio extracted to /Volumes/Jellyfin/Podcasts/<channel>/
# for Audiobookshelf podcast library
[podcastable]
TrashTaste
RedLetterMedia
Fireship
hbomberguy
Nerdwriter1
SuperEyepatchWolf
TechnologyConnections
WritingonGames
PauseandSelect
Razbuten
NeverKnowsBest
ThePrimeTime
BellularNews
SecondWind
Asmongold
KBash

