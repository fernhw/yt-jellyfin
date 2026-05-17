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
FLEEKAZOID

# LIMITS - max videos kept on disk per channel
# When a new video downloads and count exceeds limit,
# oldest videos are deleted (bottom out, top in)
# Channels not listed here: unlimited (keep all)
# Format: handle = number
[limits]
AsmonTV = 20
CDawgVA = 20
GarntM = 20
Yessenia = 15
ConnorDawg = 20
GameLinked = 20
techlinked = 20
TechDweeb = 20
RetroGameCorps = 15
DigitalFoundry = 10
SkillUp = 8
GamersNexus = 10
PapaMeat = 6
SecondWindGroup = 15
CorridorCrew = 6
InternetTodayTV = 10
ScottsStash = 10
NintendoAmerica = 15
ModernVintageGamer = 5
LGR = 5
jakkuh_t = 5

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
RetroGameCorps = 1080
SkillUp = 1080
GamersNexus = 1080
PapaMeat = 1080
SecondWindGroup = 1080
CorridorCrew = 1080
InternetTodayTV = 1080
ScottsStash = 1080
NintendoAmerica = 1080
ModernVintageGamer = 1080
LGR = 1080
jakkuh_t = 1080

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
KBash
RetroGameCorps
shoe0nhead
DrewGooden
NakeyJakey
AsmonTV
FritangaPlays
techlinked
TechDweeb
GameLinked
SchaffrillasProductions

# FORCE PODCAST - skip the open-in picker, go straight to AudioBooth
# Add channels here if you always want them to open in AudioBooth (no choice prompt)
[forcePodcast]
TrashTaste
RedLetterMedia
ThePrimeTime
BellularNews
