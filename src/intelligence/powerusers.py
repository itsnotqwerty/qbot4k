from __future__ import annotations

import sqlite3
from dataclasses import dataclass


SOCIAL_SCORE_MIN = 350
SOCIAL_SCORE_MAX = 900
SOCIAL_SCORE_DEFAULT = 500
POWERUSER_THRESHOLD = 700

_MAX_SCORE_DEFAULT_HANDLES = {
	"apollyon",
	"its_not_qwerty",
}

_FIXED_SOCIAL_SCORE_BY_NAME = {
	"apollyon": SOCIAL_SCORE_MAX,
	"its_not_qwerty": SOCIAL_SCORE_MAX,
}

_POSITIVE_TERMS = {
	"thanks",
	"thank you",
	"great",
	"awesome",
	"nice",
	"love",
	"good job",
	"well done",
}

# Slurs and explicit ToS violations that warrant automatic moderation in addition to a
# reputation penalty. All terms here are also present in _VERY_NEGATIVE_TERMS so that
# reputation scoring requires no separate check.
_EGREGIOUS_TERMS = {
	"alligatorbait", "gatorbait",
	"beaner", "bohunk",
	"boong", "boonga", "boonie", "bountybar",
	"cameljockey",
	"chink", "chinky",
	"coon", "coondog",
	"dago", "darkie", "darky", "datnigga",
	"faggot", "fagot",
	"gook", "greaseball",
	"hebe", "heeb", "honkey", "honky", "hymie",
	"ikey",
	"jap",
	"jiga", "jigaboo", "jigg", "jigga", "jiggabo", "jigger", "jijjiboo",
	"junglebunny",
	"kaffer", "kaffir", "kaffre", "kafir", "kanake", "kigger",
	"kike", "kyke", "kkk",
	"lynch",
	"macaca", "mgger", "mggor", "mooncricket", "mulatto", "munt",
	"nazi",
	"negro", "negroes", "negroid", "negro's",
	"nig", "nigg", "nigga", "niggah", "niggaracci", "niggaz",
	"nigger", "niggerhead", "niggerhole", "niggers", "nigger's",
	"niggor", "niggur", "niglet", "nignog", "nigr", "nigra", "nigre",
	"nlgger", "nlggor",
	"nip",
	"paki", "palesimian",
	"pickaninny", "picaninny", "piccaninny",
	"piker", "pikey", "piky",
	"polack", "porchmonkey",
	"raghead",
	"rape", "raped", "raper", "rapist",
	"roundeye",
	"sandnigger", "slant", "slanteye", "snownigger",
	"spaghettibender", "spaghettinigger",
	"spic", "spick", "spig", "spigotty", "spik",
	"swastika",
	"tarbaby", "timbernigger", "towelhead",
	"wetback", "whigger", "wigger",
	"wog", "wop",
	"yellowman", "zigabo", "zipperhead",
}

_VERY_NEGATIVE_TERMS = {
	"abbo", "abo", "abortion", "abuse", "addict", "addicts", "adult", "africa",
	"african", "alla", "allah", "alligatorbait", "amateur", "american", "anal", "analannie",
	"analsex", "angie", "angry", "anus", "arab", "arabs", "areola", "argie",
	"aroused", "arse", "arsehole", "asian", "ass", "assassin", "assassinate", "assassination",
	"assault", "assbagger", "assblaster", "assclown", "asscowboy", "asses", "assfuck", "assfucker",
	"asshat", "asshole", "assholes", "asshore", "assjockey", "asskiss", "asskisser", "assklown",
	"asslick", "asslicker", "asslover", "assman", "assmonkey", "assmunch", "assmuncher", "asspacker",
	"asspirate", "asspuppies", "assranger", "asswhore", "asswipe", "athletesfoot", "attack", "australian",
	"babe", "babies", "backdoor", "backdoorman", "backseat", "badfuck", "balllicker", "balls",
	"ballsack", "banging", "baptist", "barelylegal", "barf", "barface", "barfface", "bast",
	"bastard", "bazongas", "bazooms", "beaner", "beast", "beastality", "beastial", "beastiality",
	"beatoff", "beat-off", "beatyourmeat", "beaver", "bestial", "bestiality", "bi", "biatch",
	"bible", "bicurious", "bigass", "bigbastard", "bigbutt", "bigger", "bisexual", "bi-sexual",
	"bitch", "bitcher", "bitches", "bitchez", "bitchin", "bitching", "bitchslap", "bitchy",
	"biteme", "black", "blackman", "blackout", "blacks", "blind", "blow", "blowjob",
	"boang", "bogan", "bohunk", "bollick", "bollock", "bomb", "bombers", "bombing",
	"bombs", "bomd", "bondage", "boner", "bong", "boob", "boobies", "boobs",
	"booby", "boody", "boom", "boong", "boonga", "boonie", "booty", "bootycall",
	"bountybar", "bra", "brea5t", "breast", "breastjob", "breastlover", "breastman", "brothel",
	"bugger", "buggered", "buggery", "bullcrap", "bulldike", "bulldyke", "bullshit", "bumblefuck",
	"bumfuck", "bunga", "bunghole", "buried", "burn", "butchbabes", "butchdike", "butchdyke",
	"butt", "buttbang", "butt-bang", "buttface", "buttfuck", "butt-fuck", "buttfucker", "butt-fucker",
	"buttfuckers", "butt-fuckers", "butthead", "buttman", "buttmunch", "buttmuncher", "buttpirate", "buttplug",
	"buttstain", "byatch", "cacker", "cameljockey", "cameltoe", "canadian", "cancer", "carpetmuncher",
	"carruth", "catholic", "catholics", "cemetery", "chav", "cherrypopper", "chickslick", "children's",
	"chin", "chinaman", "chinamen", "chinese", "chink", "chinky", "choad", "chode",
	"christ", "christian", "church", "cigarette", "cigs", "clamdigger", "clamdiver", "clit",
	"clitoris", "clogwog", "cocaine", "cock", "cockblock", "cockblocker", "cockcowboy", "cockfight",
	"cockhead", "cockknob", "cocklicker", "cocklover", "cocknob", "cockqueen", "cockrider", "cocksman",
	"cocksmith", "cocksmoker", "cocksucer", "cocksuck", "cocksucked", "cocksucker", "cocksucking", "cocktail",
	"cocktease", "cocky", "cohee", "coitus", "color", "colored", "coloured", "commie",
	"communist", "condom", "conservative", "conspiracy", "coolie", "cooly", "coon", "coondog",
	"copulate", "cornhole", "corruption", "cra5h", "crabs", "crack", "crackpipe", "crackwhore",
	"crack-whore", "crap", "crapola", "crapper", "crappy", "crash", "creamy", "crime",
	"crimes", "criminal", "criminals", "crotch", "crotchjockey", "crotchmonkey", "crotchrot", "cum",
	"cumbubble", "cumfest", "cumjockey", "cumm", "cummer", "cumming", "cumquat", "cumqueen",
	"cumshot", "cunilingus", "cunillingus", "cunn", "cunnilingus", "cunntt", "cunt", "cunteyed",
	"cuntfuck", "cuntfucker", "cuntlick", "cuntlicker", "cuntlicking", "cuntsucker", "cybersex", "cyberslimer",
	"dago", "dahmer", "dammit", "damn", "damnation", "damnit", "darkie", "darky",
	"datnigga", "dead", "deapthroat", "death", "deepthroat", "defecate", "dego", "demon",
	"deposit", "desire", "destroy", "deth", "devil", "devilworshipper", "dick", "dickbrain",
	"dickforbrains", "dickhead", "dickless", "dicklick", "dicklicker", "dickman", "dickwad", "dickweed",
	"diddle", "die", "died", "dies", "dike", "dildo", "dingleberry", "dink",
	"dipshit", "dipstick", "dirty", "disease", "diseases", "disturbed", "dive", "dix",
	"dixiedike", "dixiedyke", "doggiestyle", "doggystyle", "dong", "doodoo", "doo-doo", "doom",
	"dope", "dragqueen", "dragqween", "dripdick", "drug", "drunk", "drunken", "dumb",
	"dumbass", "dumbbitch", "dumbfuck", "dyefly", "dyke", "easyslut", "eatballs", "eatme",
	"eatpussy", "ecstacy", "ejaculate", "ejaculated", "ejaculating", "ejaculation", "enema", "enemy",
	"erect", "erection", "ero", "escort", "ethiopian", "ethnic", "european", "evl",
	"excrement", "execute", "executed", "execution", "executioner", "explosion", "facefucker", "faeces",
	"fag", "fagging", "faggot", "fagot", "failed", "failure", "fairies", "fairy",
	"faith", "fannyfucker", "fart", "farted", "farting", "farty", "fastfuck", "fat",
	"fatah", "fatass", "fatfuck", "fatfucker", "fatso", "fckcum", "fear", "feces",
	"felatio", "felch", "felcher", "felching", "fellatio", "feltch", "feltcher", "feltching",
	"fetish", "fight", "filipina", "filipino", "fingerfood", "fingerfuck", "fingerfucked", "fingerfucker",
	"fingerfuckers", "fingerfucking", "fire", "firing", "fister", "fistfuck", "fistfucked", "fistfucker",
	"fistfucking", "fisting", "flange", "flasher", "flatulence", "floo", "flydie", "flydye",
	"fok", "fondle", "footaction", "footfuck", "footfucker", "footlicker", "footstar", "fore",
	"foreskin", "forni", "fornicate", "foursome", "fourtwenty", "fraud", "freakfuck", "freakyfucker",
	"freefuck", "fu", "fubar", "fuc", "fucck", "fuck", "fucka", "fuckable",
	"fuckbag", "fuckbuddy", "fucked", "fuckedup", "fucker", "fuckers", "fuckface", "fuckfest",
	"fuckfreak", "fuckfriend", "fuckhead", "fuckher", "fuckin", "fuckina", "fucking", "fuckingbitch",
	"fuckinnuts", "fuckinright", "fuckit", "fuckknob", "fuckme", "fuckmehard", "fuckmonkey", "fuckoff",
	"fuckpig", "fucks", "fucktard", "fuckwhore", "fuckyou", "fudgepacker", "fugly", "fuk",
	"fuks", "funeral", "funfuck", "fungus", "fuuck", "gangbang", "gangbanged", "gangbanger",
	"gangsta", "gatorbait", "gay", "gaymuthafuckinwhore", "gaysex", "geez", "geezer", "geni",
	"genital", "genocide", "german", "getiton", "gin", "ginzo", "gipp", "girls", "givehead",
	"glazeddonut", "gob", "god", "godammit", "goddamit", "goddammit", "goddamn", "goddamned",
	"goddamnes", "goddamnit", "goddamnmuthafucker", "goldenshower", "gonorrehea", "gonzagas", "gook", "gotohell",
	"goy", "goyim", "greaseball", "gringo", "groe", "gross", "grostulation", "gubba",
	"gummer", "gun", "gyp", "gypo", "gypp", "gyppie", "gyppo", "gyppy",
	"hamas", "handjob", "hapa", "harder", "hardon", "harem", "headfuck", "headlights",
	"hebe", "heeb", "hell", "henhouse", "heroin", "herpes", "heterosexual", "hijack",
	"hijacker", "hijacking", "hillbillies", "hindoo", "hiscock", "hitler", "hitlerism", "hitlerist",
	"hiv", "ho", "hobo", "hodgie", "hoes", "hole", "holestuffer", "homicide",
	"homo", "homobangers", "homosexual", "honger", "honk", "honkers", "honkey", "honky",
	"hook", "hooker", "hookers", "hooters", "hore", "hork", "horn", "horney",
	"horniest", "horny", "horseshit", "hosejob", "hoser", "hostage", "hotdamn", "hotpussy",
	"hottotrot", "hummer", "husky", "hussy", "hustler", "hymen", "hymie", "iblowu",
	"idiot", "ikey", "illegal", "incest", "insest", "intercourse", "interracial", "intheass",
	"inthebuff", "israel", "israeli", "israel's", "italiano", "itch", "jackass", "jackoff",
	"jackshit", "jacktheripper", "jade", "jap", "japanese", "japcrap", "jebus", "jeez",
	"jerkoff", "jesus", "jesuschrist", "jew", "jewish", "jiga", "jigaboo", "jigg",
	"jigga", "jiggabo", "jigger", "jiggy", "jihad", "jijjiboo", "jimfish", "jism",
	"jiz", "jizim", "jizjuice", "jizm", "jizz", "jizzim", "jizzum", "joint",
	"juggalo", "jugs", "junglebunny", "kaffer", "kaffir", "kaffre", "kafir", "kanake",
	"kid", "kigger", "kike", "kill", "killed", "killer", "killing", "kills",
	"kink", "kinky", "kissass", "kkk", "knife", "knockers", "kock", "kondum",
	"koon", "kotex", "krap", "krappy", "kraut", "kum", "kumbubble", "kumbullbe",
	"kummer", "kumming", "kumquat", "kums", "kunilingus", "kunnilingus", "kunt", "ky",
	"kyke", "lactate", "laid", "lapdance", "latin", "lesbain", "lesbayn", "lesbian",
	"lesbin", "lesbo", "lez", "lezbe", "lezbefriends", "lezbo", "lezz", "lezzo",
	"liberal", "libido", "licker", "lickme", "lies", "limey", "limpdick", "limy",
	"lingerie", "liquor", "livesex", "loadedgun", "lolita", "looser", "loser", "lotion",
	"lovebone", "lovegoo", "lovegun", "lovejuice", "lovemuscle", "lovepistol", "loverocket", "lowlife",
	"lsd", "lubejob", "lucifer", "luckycammeltoe", "lugan", "lynch", "macaca", "mad",
	"mafia", "magicwand", "mams", "manhater", "manpaste", "marijuana", "mastabate", "mastabater",
	"masterbate", "masterblaster", "mastrabator", "masturbate", "masturbating", "mattressprincess", "meatbeatter", "meatrack",
	"meth", "mexican", "mgger", "mggor", "mickeyfinn", "mideast", "milf", "minority",
	"mockey", "mockie", "mocky", "mofo", "moky", "moles", "molest", "molestation",
	"molester", "molestor", "moneyshot", "mooncricket", "mormon", "moron", "moslem", "mosshead",
	"mothafuck", "mothafucka", "mothafuckaz", "mothafucked", "mothafucker", "mothafuckin", "mothafucking", "mothafuckings",
	"motherfuck", "motherfucked", "motherfucker", "motherfuckin", "motherfucking", "motherfuckings", "motherlovebone", "muff",
	"muffdive", "muffdiver", "muffindiver", "mufflikcer", "mulatto", "muncher", "munt", "murder",
	"murderer", "muslim", "naked", "narcotic", "nasty", "nastybitch", "nastyho", "nastyslut",
	"nastywhore", "nazi", "necro", "negro", "negroes", "negroid", "negro's", "nig",
	"niger", "nigerian", "nigerians", "nigg", "nigga", "niggah", "niggaracci", "niggard",
	"niggarded", "niggarding", "niggardliness", "niggardliness's", "niggardly", "niggards", "niggard's", "niggaz",
	"nigger", "niggerhead", "niggerhole", "niggers", "nigger's", "niggle", "niggled", "niggles",
	"niggling", "nigglings", "niggor", "niggur", "niglet", "nignog", "nigr", "nigra",
	"nigre", "nip", "nipple", "nipplering", "nittit", "nlgger", "nlggor", "nofuckingway",
	"nook", "nookey", "nookie", "noonan", "nooner", "nude", "nudger", "nuke",
	"nutfucker", "nymph", "ontherag", "oral", "orga", "orgasim", "orgasm", "orgies",
	"orgy", "osama", "paki", "palesimian", "palestinian", "pansies", "pansy", "panti",
	"panties", "payo", "pearlnecklace", "peck", "pecker", "peckerwood", "pee", "peehole",
	"pee-pee", "peepshow", "peepshpw", "pendy", "penetration", "peni5", "penile", "penis",
	"penises", "penthouse", "period", "perv", "phonesex", "phuk", "phuked", "phuking",
	"phukked", "phukking", "phungky", "phuq", "pi55", "picaninny", "piccaninny", "pickaninny",
	"piker", "pikey", "piky", "pimp", "pimped", "pimper", "pimpjuic", "pimpjuice",
	"pimpsimp", "pindick", "piss", "pissed", "pisser", "pisses", "pisshead", "pissin",
	"pissing", "pissoff", "pistol", "pixie", "pixy", "playboy", "playgirl", "pocha",
	"pocho", "pocketpool", "pohm", "polack", "pom", "pommie", "pommy", "poo",
	"poon", "poontang", "poop", "pooper", "pooperscooper", "pooping", "poorwhitetrash", "popimp",
	"porchmonkey", "porn", "pornflick", "pornking", "porno", "pornography", "pornprincess", "pot",
	"poverty", "premature", "pric", "prick", "prickhead", "primetime", "propaganda", "pros",
	"prostitute", "protestant", "pu55i", "pu55y", "pube", "pubic", "pubiclice", "pud",
	"pudboy", "pudd", "puddboy", "puke", "puntang", "purinapricness", "puss", "pussie",
	"pussies", "pussy", "pussycat", "pussyeater", "pussyfucker", "pussylicker", "pussylips", "pussylover",
	"pussypounder", "pusy", "quashie", "queef", "queer", "quickie", "quim", "ra8s",
	"rabbi", "racial", "racist", "radical", "radicals", "raghead", "randy", "rape",
	"raped", "raper", "rapist", "rearend", "rearentry", "rectum", "redlight", "redneck",
	"reefer", "reestie", "refugee", "reject", "remains", "rentafuck", "republican", "rere",
	"retard", "retarded", "ribbed", "rigger", "rimjob", "rimming", "roach", "robber",
	"roundeye", "rump", "russki", "russkie", "sadis", "sadom", "samckdaddy", "sandm",
	"sandnigger", "satan", "scag", "scallywag", "scat", "schlong", "screw", "screwyou",
	"scrotum", "scum", "semen", "seppo", "servant", "sex", "sexed", "sexfarm",
	"sexhound", "sexhouse", "sexing", "sexkitten", "sexpot", "sexslave", "sextogo", "sextoy",
	"sextoys", "sexual", "sexually", "sexwhore", "sexy", "sexymoma", "sexy-slim", "shag",
	"shaggin", "shagging", "shat", "shav", "shawtypimp", "sheeney", "shhit", "shinola",
	"shit", "shitcan", "shitdick", "shite", "shiteater", "shited", "shitface", "shitfaced",
	"shitfit", "shitforbrains", "shitfuck", "shitfucker", "shitfull", "shithapens", "shithappens", "shithead",
	"shithouse", "shiting", "shitlist", "shitola", "shitoutofluck", "shits", "shitstain", "shitted",
	"shitter", "shitting", "shitty", "shoot", "shooting", "shortfuck", "showtime", "sick",
	"sissy", "sixsixsix", "sixtynine", "sixtyniner", "skank", "skankbitch", "skankfuck", "skankwhore",
	"skanky", "skankybitch", "skankywhore", "skinflute", "skum", "skumbag", "slant", "slanteye",
	"slapper", "slaughter", "slav", "slave", "slavedriver", "sleezebag", "sleezeball", "slideitin",
	"slime", "slimeball", "slimebucket", "slopehead", "slopey", "slopy", "slut", "sluts",
	"slutt", "slutting", "slutty", "slutwear", "slutwhore", "smack", "smackthemonkey", "smut",
	"snatch", "snatchpatch", "snigger", "sniggered", "sniggering", "sniggers", "snigger's", "sniper",
	"snot", "snowback", "snownigger", "sob", "sodom", "sodomise", "sodomite", "sodomize",
	"sodomy", "sonofabitch", "sonofbitch", "sooty", "sos", "soviet", "spaghettibender", "spaghettinigger",
	"spank", "spankthemonkey", "sperm", "spermacide", "spermbag", "spermhearder", "spermherder", "spic",
	"spick", "spig", "spigotty", "spik", "spit", "spitter", "splittail", "spooge",
	"spreadeagle", "spunk", "spunky", "squaw", "stagg", "stiffy", "strapon", "stringer",
	"stripclub", "stroke", "stroking", "stupid", "stupidfuck", "stupidfucker", "suck", "suckdick",
	"sucker", "suckme", "suckmyass", "suckmydick", "suckmytit", "suckoff", "suicide", "swallow",
	"swallower", "swalow", "swastika", "sweetness", "syphilis", "taboo", "taff", "tampon",
	"tang", "tantra", "tarbaby", "tard", "teat", "terror", "terrorist", "teste",
	"testicle", "testicles", "thicklips", "thirdeye", "thirdleg", "threesome", "threeway", "timbernigger",
	"tinkle", "tit", "titbitnipply", "titfuck", "titfucker", "titfuckin", "titjob", "titlicker",
	"titlover", "tits", "tittie", "titties", "titty", "tnt", "toilet", "tongethruster",
	"tongue", "tonguethrust", "tonguetramp", "tortur", "torture", "tosser", "towelhead", "trailertrash",
	"tramp", "trannie", "tranny", "transexual", "transsexual", "transvestite", "triplex", "trisexual",
	"trojan", "trots", "tuckahoe", "tunneloflove", "turd", "turnon", "twat", "twink",
	"twinkie", "twobitwhore", "uck", "uk", "unfuckable", "upskirt", "uptheass", "upthebutt",
	"urinary", "urinate", "urine", "usama", "uterus", "vagina", "vaginal", "vatican",
	"vibr", "vibrater", "vibrator", "vietcong", "violence", "virgin", "virginbreaker", "vomit",
	"vulva", "wab", "wank", "wanker", "wanking", "waysted", "weapon", "weenie",
	"weewee", "welcher", "welfare", "wetb", "wetback", "wetspot", "whacker", "whash",
	"whigger", "whiskey", "whiskeydick", "whiskydick", "whit", "whitenigger", "whites", "whitetrash",
	"whitey", "whiz", "whop", "whore", "whorefucker", "whorehouse", "wigger", "willie",
	"williewanker", "willy", "wn", "wog", "women's", "wop", "wuss",
	"wuzzie", "xtc", "xxx", "yankee", "yellowman", "zigabo", "zipperhead",
}


def clamp_social_score(score: int) -> int:
	return max(SOCIAL_SCORE_MIN, min(SOCIAL_SCORE_MAX, score))


def is_poweruser_score(score: int) -> bool:
	return clamp_social_score(score) >= POWERUSER_THRESHOLD


def average_social_scores(first_score: int, second_score: int) -> int:
	return clamp_social_score(int(round((first_score + second_score) / 2)))


def default_social_score_for_name(display_name: str) -> int:
	normalized = display_name.strip().casefold()
	if normalized in _MAX_SCORE_DEFAULT_HANDLES:
		return SOCIAL_SCORE_MAX
	return SOCIAL_SCORE_DEFAULT


def enforced_social_score_for_name(display_name: str, proposed_score: int) -> int:
	normalized = display_name.strip().casefold()
	if normalized in _FIXED_SOCIAL_SCORE_BY_NAME:
		return int(_FIXED_SOCIAL_SCORE_BY_NAME[normalized])
	return clamp_social_score(proposed_score)


def score_delta_for_message(content_raw: str) -> tuple[int, str] | None:
	normalized = content_raw.casefold().strip()
	if not normalized:
		return None
	if normalized.startswith("!") or normalized.startswith("/"):
		return None

	for term in _VERY_NEGATIVE_TERMS:
		if term in normalized:
			return (-10, "very_negative_content")

	for term in _POSITIVE_TERMS:
		if term in normalized:
			return (1, "positive_message")

	return (1, "message_sent")


def is_egregious_content(content: str) -> bool:
	normalized = content.casefold().strip()
	return any(term in normalized for term in _EGREGIOUS_TERMS)


def score_delta_for_moderation(*, severity: str, action_type: str | None = None) -> tuple[int, str]:
	severity_key = severity.casefold().strip()
	base_delta = {
		"low": -20,
		"medium": -35,
		"high": -55,
	}.get(severity_key, -25)
	if action_type:
		base_delta -= 15
	return (base_delta, "moderation_penalty")


@dataclass(frozen=True)
class ReputationUpdate:
	user_id: int
	delta: int
	current_score: int
	candidate_flag: bool
	reason_code: str


def apply_reputation_event(
	connection: sqlite3.Connection,
	*,
	user_id: int,
	delta: int,
	reason_code: str,
	source_type: str,
	source_id: int | None = None,
	candidate_threshold: int = POWERUSER_THRESHOLD,
	minimum_score: int = SOCIAL_SCORE_MIN,
	maximum_score: int = SOCIAL_SCORE_MAX,
) -> ReputationUpdate:
	user = connection.execute(
		"""
		SELECT id, primary_display_name, current_reputation_score
		FROM users
		WHERE id = ?
		""",
		(user_id,),
	).fetchone()
	if user is None:
		raise ValueError("canonical user not found")

	current_score = int(user[2])
	updated_score = max(minimum_score, min(maximum_score, current_score + delta))
	updated_score = enforced_social_score_for_name(str(user[1]), updated_score)
	candidate_flag = updated_score >= candidate_threshold

	with connection:
		connection.execute(
			"""
			INSERT INTO reputation_events (
				user_id,
				source_type,
				source_id,
				delta,
				reason_code
			) VALUES (?, ?, ?, ?, ?)
			""",
			(user_id, source_type, source_id, delta, reason_code),
		)
		connection.execute(
			"""
			UPDATE users
			SET current_reputation_score = ?,
			    candidate_flag = ?,
			    updated_at = CURRENT_TIMESTAMP
			WHERE id = ?
			""",
			(updated_score, int(candidate_flag), user_id),
		)
		connection.execute(
			"""
			INSERT INTO audit_log (
				actor_type,
				actor_id,
				action_type,
				entity_type,
				entity_id,
				payload_json
			) VALUES (
				'system',
				NULL,
				'user_reputation_update',
				'user',
				?,
				json_object('delta', ?, 'reason_code', ?, 'source_type', ?, 'source_id', ?)
			)
			""",
			(user_id, delta, reason_code, source_type, source_id),
		)

	return ReputationUpdate(
		user_id=user_id,
		delta=delta,
		current_score=updated_score,
		candidate_flag=candidate_flag,
		reason_code=reason_code,
	)


def get_reputation_history(
	connection: sqlite3.Connection,
	user_id: int,
) -> list[sqlite3.Row]:
	rows = connection.execute(
		"""
		SELECT id, source_type, source_id, delta, reason_code, created_at
		FROM reputation_events
		WHERE user_id = ?
		ORDER BY created_at, id
		""",
		(user_id,),
	).fetchall()
	return list(rows)
