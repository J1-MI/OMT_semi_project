import requests, lxml.html as LH
u = 'https://kittyforums.to/combolists'
h = {'User-Agent': 'Mozilla/5.0'}
r = requests.get(u, headers=h, timeout=20)
doc = LH.fromstring(r.text)
links = [a.get('href') for a in doc.cssselect('li.title-list-item a[href*="/thread/"]')]
print('status:', r.status_code, 'threads:', len(links))
print(links[:10])
