"""
What a customer is told when their question goes to the team, in their own language.

Fixed sentences, never written by a model: this is the moment a model has nothing true to say, and a sentence that
cannot vary cannot invent a fee. English has no entry, because each channel keeps its own English wording, which can
name the missing fact. Language names are those of shared/team/language.py. Marathi is read as Hindi there.
"""

_TOLD = {
    "hinglish": "Iski pakki jaankari abhi mere paas nahi hai. Maine team ko bata diya hai, woh aapse sampark karegi.",
    "hindi": "इसकी पक्की जानकारी अभी मेरे पास नहीं है। मैंने टीम को बता दिया है, टीम आपसे संपर्क करेगी।",
    "tamil": "இதைப் பற்றிய உறுதியான தகவல் இப்போது என்னிடம் இல்லை. குழுவிடம் தெரிவித்துவிட்டேன், அவர்கள் உங்களைத் தொடர்பு கொள்வார்கள்.",
    "telugu": "దీని గురించి ఖచ్చితమైన సమాచారం ఇప్పుడు నా దగ్గర లేదు. టీమ్‌కి చెప్పాను, వాళ్ళు మిమ్మల్ని సంప్రదిస్తారు.",
    "bengali": "এ বিষয়ে নিশ্চিত তথ্য এখন আমার কাছে নেই। আমি টিমকে জানিয়ে দিয়েছি, তাঁরা আপনার সঙ্গে যোগাযোগ করবেন।",
    "gujarati": "આ વિશે પાકી માહિતી અત્યારે મારી પાસે નથી. મેં ટીમને જણાવી દીધું છે, તેઓ તમારો સંપર્ક કરશે.",
    "kannada": "ಇದರ ಬಗ್ಗೆ ಖಚಿತ ಮಾಹಿತಿ ಈಗ ನನ್ನ ಬಳಿ ಇಲ್ಲ. ತಂಡಕ್ಕೆ ತಿಳಿಸಿದ್ದೇನೆ, ಅವರು ನಿಮ್ಮನ್ನು ಸಂಪರ್ಕಿಸುತ್ತಾರೆ.",
    "malayalam": "ഇതിനെക്കുറിച്ച് ഉറപ്പായ വിവരം ഇപ്പോൾ എന്റെ കയ്യിലില്ല. ടീമിനെ അറിയിച്ചിട്ടുണ്ട്, അവർ നിങ്ങളെ ബന്ധപ്പെടും.",
    "punjabi": "ਇਸ ਬਾਰੇ ਪੱਕੀ ਜਾਣਕਾਰੀ ਹੁਣ ਮੇਰੇ ਕੋਲ ਨਹੀਂ ਹੈ। ਮੈਂ ਟੀਮ ਨੂੰ ਦੱਸ ਦਿੱਤਾ ਹੈ, ਉਹ ਤੁਹਾਡੇ ਨਾਲ ਸੰਪਰਕ ਕਰਨਗੇ।",
    "odia": "ଏ ବିଷୟରେ ନିଶ୍ଚିତ ତଥ୍ୟ ଏବେ ମୋ ପାଖରେ ନାହିଁ। ମୁଁ ଟିମକୁ ଜଣାଇଦେଇଛି, ସେମାନେ ଆପଣଙ୍କ ସହ ଯୋଗାଯୋଗ କରିବେ।",
}

# The same call, a later question: the full promise once, then something short.
_TOLD_AGAIN = {
    "hinglish": "Ye bhi team ko bata diya hai. Aur kuch poochna hai?",
    "hindi": "यह भी टीम को बता दिया है। और कुछ पूछना है?",
    "tamil": "இதையும் குழுவிடம் தெரிவித்துவிட்டேன். வேறு ஏதாவது கேட்க வேண்டுமா?",
    "telugu": "ఇది కూడా టీమ్‌కి చెప్పాను. ఇంకేమైనా అడగాలా?",
    "bengali": "এটাও টিমকে জানিয়ে দিয়েছি। আর কিছু জানতে চান?",
    "gujarati": "આ પણ ટીમને જણાવી દીધું છે. બીજું કંઈ પૂછવું છે?",
    "kannada": "ಇದನ್ನೂ ತಂಡಕ್ಕೆ ತಿಳಿಸಿದ್ದೇನೆ. ಬೇರೆ ಏನಾದರೂ ಕೇಳಬೇಕೇ?",
    "malayalam": "ഇതും ടീമിനെ അറിയിച്ചിട്ടുണ്ട്. വേറെ എന്തെങ്കിലും ചോദിക്കാനുണ്ടോ?",
    "punjabi": "ਇਹ ਵੀ ਟੀਮ ਨੂੰ ਦੱਸ ਦਿੱਤਾ ਹੈ। ਹੋਰ ਕੁਝ ਪੁੱਛਣਾ ਹੈ?",
    "odia": "ଏହା ମଧ୍ୟ ଟିମକୁ ଜଣାଇଦେଇଛି। ଆଉ କିଛି ପଚାରିବାକୁ ଅଛି କି?",
}

# Nobody can get back to them yet: a website visitor or an Instagram handle with no number.
_NEEDS_CONTACT = {
    "hinglish": "Is baare mein team aapki madad karegi. Apna naam aur phone number bata dijiye.",
    "hindi": "इस बारे में टीम आपकी मदद करेगी। अपना नाम और फ़ोन नंबर बता दीजिए।",
    "tamil": "இதில் குழு உங்களுக்கு உதவும். உங்கள் பெயரையும் தொலைபேசி எண்ணையும் சொல்லுங்கள்.",
    "telugu": "దీనిలో టీమ్ మీకు సహాయం చేస్తుంది. మీ పేరు, ఫోన్ నంబర్ చెప్పండి.",
    "bengali": "এ বিষয়ে টিম আপনাকে সাহায্য করবে। আপনার নাম আর ফোন নম্বর জানান।",
    "gujarati": "આ વિશે ટીમ તમને મદદ કરશે. તમારું નામ અને ફોન નંબર જણાવો.",
    "kannada": "ಇದರಲ್ಲಿ ತಂಡ ನಿಮಗೆ ಸಹಾಯ ಮಾಡುತ್ತದೆ. ನಿಮ್ಮ ಹೆಸರು ಮತ್ತು ಫೋನ್ ನಂಬರ್ ತಿಳಿಸಿ.",
    "malayalam": "ഇതിൽ ടീം നിങ്ങളെ സഹായിക്കും. നിങ്ങളുടെ പേരും ഫോൺ നമ്പറും അറിയിക്കൂ.",
    "punjabi": "ਇਸ ਬਾਰੇ ਟੀਮ ਤੁਹਾਡੀ ਮਦਦ ਕਰੇਗੀ। ਆਪਣਾ ਨਾਮ ਅਤੇ ਫ਼ੋਨ ਨੰਬਰ ਦੱਸੋ।",
    "odia": "ଏ ବିଷୟରେ ଟିମ ଆପଣଙ୍କୁ ସାହାଯ୍ୟ କରିବେ। ଆପଣଙ୍କ ନାମ ଓ ଫୋନ ନମ୍ବର ଜଣାନ୍ତୁ।",
}


def told(language: str | None) -> str | None:
    return _TOLD.get(language or "")


def told_again(language: str | None) -> str | None:
    return _TOLD_AGAIN.get(language or "")


def needs_contact(language: str | None) -> str | None:
    return _NEEDS_CONTACT.get(language or "")
