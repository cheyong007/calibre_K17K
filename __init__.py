#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = '2019, Yohann Che<cheyong007@live.com>'
__docformat__ = 'restructuredtext en'

import socket, time, re
from threading import Thread
from Queue import Queue, Empty
from urllib import quote

from calibre import as_unicode, random_user_agent
from calibre.ebooks.metadata import check_isbn
from calibre.ebooks.metadata.sources.base import (Source, Option, fixcase,
        fixauthors)
from calibre.ebooks.metadata.book.base import Metadata
from calibre.utils.localization import canonicalize_lang

class K17K(Source):

    name = '17k.com'
    description = 'Downloads metadata and covers from 17k.com. Only for online books without ISBN.'

    author = 'Yohann Che'
    version = (0, 1, 0)
    minimum_calibre_version = (0, 8, 0)
    
    '''
    Calibre的元数据集合
    PUBLICATION_METADATA_FIELDS = frozenset((
        'title',            # title must never be None. Should be _('Unknown')
        # Pseudo field that can be set, but if not set is auto generated
        # from title and languages
        'title_sort',
        'authors',          # Ordered list. Must never be None, can be [_('Unknown')]
        'author_sort_map',  # Map of sort strings for each author
        # Pseudo field that can be set, but if not set is auto generated
        # from authors and languages
        'author_sort',
        'book_producer',
        'timestamp',        # Dates and times must be timezone aware
        'pubdate',
        'last_modified',
        'rights',
        # So far only known publication type is periodical:calibre
        # If None, means book
        'publication_type',
        'uuid',             # A UUID usually of type 4
        'languages',        # ordered list of languages in this publication
        'publisher',        # Simple string, no special semantics
        # Absolute path to image file encoded in filesystem_encoding
        'cover',
        # Of the form (format, data) where format is, for e.g. 'jpeg', 'png', 'gif'...
        'cover_data',
        # Either thumbnail data, or an object with the attribute
        # image_path which is the path to an image file, encoded
        # in filesystem_encoding
        'thumbnail',
    ))
    '''
    # 17K提供的元数据信息
    # (cover,title,author, series, tags, comments, date_updated, book_id,book_url)

    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset(['title', 'authors', 'identifier:amazon_cn',
        'rating', 'comments', 'publisher', 'pubdate',
        'languages', 'series'])
    has_html_comments = True
    supports_gzip_transfer_encoding = True
    prefer_results_with_isbn = False

    MAX_EDITIONS = 5
    BASE_URL = r"http://www.17k.com"
    BOOK_URL = r"http://www.17k.com/book/"
    SEARCH_URL = r"https://search.17k.com/search.xhtml?c.st=0&c.q="
    
    idtype = '17k'

    def __init__(self, *args, **kwargs):
        Source.__init__(self, *args, **kwargs)

    def test_fields(self, mi):
        '''
        Return the first field from self.touched_fields that is null on the mi object
        对mi对象从self.touched_fields中取得第一个为空值的字段
        '''
        for key in self.touched_fields:
            if key.startswith('identifier:'):
                #当字段为书号时，取得最后书号值
                key = key.partition(':')[-1]
                if not mi.has_identifier(key):
                    return 'identifier:' + key
            elif mi.is_null(key):
                return key

    @property
    def user_agent(self):
        # Pass in an index to random_user_agent() to test with a particular
        # user agent
        return random_user_agent()

    def get_asin(self, identifiers):
        '''
        从元数据中获取书号ID
        '''

        for key, val in identifiers.iteritems():
            key = key.lower()
            if key in (self.idtype, 'asin'):
                return val
        return None

    def get_book_url(self, identifiers):
        '''
        通过书号ID生成书籍详情URL
        '''
        asin = self.get_asin(identifiers)
        if asin:
            url = BOOK_URL +asin
            idtype = self.idtype
            return (idtype, asin, url)

    def get_book_url_name(self, idtype, idval, url):
        return self.name

    def clean_downloaded_metadata(self, mi):
        docase = (
            mi.language == 'eng'
        )
        if mi.title and docase:
            mi.title = fixcase(mi.title)
        mi.authors = fixauthors(mi.authors)
        if mi.tags and docase:
            mi.tags = list(map(fixcase, mi.tags))
        mi.isbn = check_isbn(mi.isbn)

    def create_query(self, log, title=None, authors=None): # {{{
        '''
        只通过title字段来生成查询的URL
        查询字段如果有空格则在URL上会被替换成‘+’
        '''
        from urllib import urlencode
        q = ""

        if title:
            # title过滤掉特殊符号：get_title_tokens
            title_tokens = list(self.get_title_tokens(title))
            if title_tokens:
                q = ' '.join(title_tokens)
#        if authors:
#            # authors过滤掉特殊符号：get_author_tokens
#            author_tokens = self.get_author_tokens(authors,
#                    only_first_author=True)
#            if author_tokens:
#                q = q + ' '.join(author_tokens)


        encode_to = 'utf-8'
        encoded_q = q.encode(encode_to)
        '''
        查询关键字有空格，则替换为“+”连接非空格字符
        '''
        url = self.SEARCH_URL + encoded_q.replace(' ', '+')
        #print(url)
        return url

    # }}}

    def get_cached_cover_url(self, identifiers):  # {{{
        url = None
        asin = self.get_asin(identifiers)
        if asin is None:
            isbn = identifiers.get('isbn', None)
            if isbn is not None:
                asin = self.cached_isbn_to_identifier(isbn)
        if asin is not None:
            url = self.cached_identifier_to_cover_url(asin)

        return url
    # }}}

    def parse_results_page(self, root):  # {{{
        '''
        解析搜索结果
        '''
        from lxml.html import tostring

        matches = []

        def title_ok(title):
            title = title.lower()
            bad = [u'套装', u'[有声书]', u'[音频cd]']
            for x in bad:
                if x in title:
                    return False
            return True

        for a in root.xpath(r'.//div[@class="textmiddle"]/dl/dt[1]/a'):
            # 获取所有搜索结果的书名
            title = tostring(a, method='text', encoding=unicode)
            if title_ok(title):
                url = a.get('href')
                if url.startswith('/'):
                    # 17K页面链接地址href都是“//...”开头，URL加上协议头
                    url = 'http:%s' % (url)
                matches.append(url)
            if not matches:
                break;
        # 保留最顶部的前MAX_EDITIONS个匹配的结果，后面的相关度不高, 17K搜索精度不高
        return matches[:self.MAX_EDITIONS]
    # }}}

    def identify(self, log, result_queue, abort, title=None, authors=None,
            identifiers={}, timeout=30):  # {{{
        '''
        Note this method will retry without identifiers automatically if no
        match is found with identifiers.
        如果使用id未找到匹配，自动不使用id重试查找匹配。
        '''
        from calibre.utils.cleantext import clean_ascii_chars
        from calibre.ebooks.chardet import xml_to_unicode
        from lxml.html import tostring
        import html5lib

        testing = getattr(self, 'running_a_test', False)

        query = self.create_query(log, title=title, authors=authors)

        if query is None:
            log.error('Insufficient metadata to construct query')
            return
        br = self.browser
        if testing:
            print ('Using user agent for 17k.com: %s'%self.user_agent)
        try:
            raw = br.open_novisit(query, timeout=timeout).read().strip()
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and \
                    e.getcode() == 404:
                log.error('Query malformed: %r'%query)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = '17k.com timed out. Try again later.'
                log.error(msg)
            else:
                msg = 'Failed to make identify query: %r'%query
                log.exception(msg)
            return as_unicode(msg)

        raw = clean_ascii_chars(xml_to_unicode(raw,
            strip_encoding_pats=True, resolve_entities=True)[0])

        if testing:
            import tempfile
            with tempfile.NamedTemporaryFile(prefix='17k_results_',
                    suffix='.html', delete=False) as f:
                f.write(raw.encode('utf-8'))
            print ('Downloaded html for results page saved in', f.name)

        matches = []
        found = '<title>404 - ' not in raw

        if found:
            try:
                root = html5lib.parse(raw, treebuilder='lxml',
                        namespaceHTMLElements=False)
            except:
                msg = 'Failed to parse 17k page for query: %r' %query
                log.exception(msg)
                return msg

                errmsg = root.xpath('//*[@id="errorMessage"]')
                if errmsg:
                    msg = tostring(errmsg, method='text', encoding=unicode).strip()
                    log.error(msg)
                    # The error is almost always a not found error
                    found = False

        if found:
            matches = self.parse_results_page(root)

        if abort.is_set():
            return

        if not matches:
            if identifiers and title and authors:
                log('No matches found with identifiers, retrying using only title and authors. Query: %r'%query)
                return self.identify(log, result_queue, abort, title=title,
                        authors=authors, timeout=timeout)
            log.error('No matches found with query: %r'%query)
            return

        from calibre_plugins.K17K.worker import Worker
        workers = [Worker(url, result_queue, br, log, i, self,
                            testing=testing) for i, url in enumerate(matches)]

        for w in workers:
            w.start()
            # Don't send all requests at the same time
            time.sleep(0.1)

        while not abort.is_set():
            a_worker_is_alive = False
            for w in workers:
                w.join(0.2)
                if abort.is_set():
                    break
                if w.is_alive():
                    a_worker_is_alive = True
            if not a_worker_is_alive:
                break

        return None
    # }}}

    def download_cover(self, log, result_queue, abort,
            title=None, authors=None, identifiers={}, timeout=30,
            get_best_cover=False):  # {{{
        cached_url = self.get_cached_cover_url(identifiers)
        if cached_url is None:
            log.info('No cached cover found, running identify')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors,
                    identifiers=identifiers)
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(key=self.identify_results_keygen(
                title=title, authors=authors, identifiers=identifiers))
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
        if cached_url is None:
            log.info('No cover found')
            return

        if abort.is_set():
            return
        br = self.browser
        log('Downloading cover from:', cached_url)
        try:
            cdata = br.open_novisit(cached_url, timeout=timeout).read()
            if cdata:
                result_queue.put((self, cdata))
        except:
            log.exception('Failed to download cover from:', cached_url)
    # }}}

if __name__ == '__main__':  # tests {{{
    # To run these test use: calibre-debug -e __init__.py
    from calibre.ebooks.metadata.sources.test import (test_identify_plugin,
            title_test, authors_test)

#    test_identify_plugin(K17K.name,
#        [
#            (
#                {'identifiers':{'17K': '37678'}},
#                [title_test('边城', exact=True),
#                 authors_test(['沈从文'])
#                ]
#            ),
#
#        ])