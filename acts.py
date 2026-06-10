import requests

headers = {
    "Accept": "application/xhtml+xml",
    "Accept-Language": "eng",
}

# AI Act
r_aiact = requests.get("http://data.europa.eu/eli/reg/2024/1689/oj", headers=headers)
# GDPR (consolidated 2018)
r_gdpr  = requests.get("http://data.europa.eu/eli/reg/2016/679/oj", headers=headers)

# Cellar returns 303 → follow redirects; requests does this automatically
print(r_aiact.status_code, len(r_aiact.text))
print(r_gdpr.status_code, len(r_gdpr.text))

from pathlib import Path
Path("aiact.html").write_text(r_aiact.text, encoding="utf-8")
Path("gdpr.html").write_text(r_gdpr.text, encoding="utf-8")