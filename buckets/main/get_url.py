import urllib.request
url = 'https://search.abb.com/library/Download.aspx?DocumentID=3ADR011510&LanguageCode=de&LanguageCode=en&LanguageCode=es&LanguageCode=fr&LanguageCode=zh&DocumentPartId=&Action=Launch&DocumentRevisionId=A'
class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        return headers
opener = urllib.request.build_opener(NoRedirectHandler())
response = opener.open(url)
with open('url.txt', 'w') as f:
    f.write(response.get('Location'))
