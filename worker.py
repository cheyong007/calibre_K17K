#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = '2019, Yohann Che<cheyong007@live.com>'
__docformat__ = 'restructuredtext en'

import socket, re, datetime
from collections import OrderedDict
from threading import Thread

from lxml.html import fromstring, tostring

from calibre.ebooks.metadata.book.base import Metadata
from calibre.library.comments import sanitize_comments_html
from calibre.utils.cleantext import clean_ascii_chars
from calibre.utils.localization import canonicalize_lang

def CSSSelect(expr):
    from cssselect import HTMLTranslator
    from lxml.etree import XPath
    return XPath(HTMLTranslator().css_to_xpath(expr))

class Worker(Thread):  # Get details {{{

    '''
    Get book details from 17k book page in a separate thread
    以独立线程获取书籍信息
    从书籍详情面获取书籍信息。
    /book/{bookid}.html
    '''

    def __init__(self, url, result_queue, browser, log, relevance, plugin,
            timeout=20, testing=False):
        Thread.__init__(self)
        self.daemon = True
        self.testing = testing
        self.url, self.result_queue = url, result_queue
        self.log, self.timeout = log, timeout
        self.relevance, self.plugin = relevance, plugin
        self.browser = browser.clone_browser()
        self.cover_url = self.k17k_id = self.isbn = None
        from lxml.html import tostring
        self.tostring = tostring

        self.cover_url_xpath = './/div[@class="cover"]/a/img'
        self.title_xpath = './/div[@class="BookInfo"]//h1/a/text()'
        self.author_xpath = './/div[@class="author"]/a[@class="name"]/text()'
        self.series_xpath = './/div[@class="infoPath"]/div/a[3]/text()'
        self.tags_xpath = './/tr[@class="label"]/td[@colspan="3"]/a/span/text()'
        self.comments_xpath = './/p[@class="intro"]/a/text()'
        self.last_modified_xpath = './/dl[@id="bookInfo"]/dt[@class="tit"]/em/text()'
        self.k17k_id_xpath = './/div[@class="infoPath"]//span/text()'
        #self.book_url_xpath = './/div[@class="textmiddle"]/dl/dt[1]/a/@href'

        self.series_pat = re.compile(
                r'''
                \|\s*              # Prefix
                (Series)\s*:\s*    # Series declaration
                (?P<series>.+?)\s+  # The series name
                \((Book)\s*    # Book declaration
                (?P<index>[0-9.]+) # Series index
                \s*\)
                ''', re.X)



    def run(self):
        try:
            self.get_details()
        except:
            self.log.exception('get_details failed for url: %r'%self.url)

    def get_details(self):
        '''
        从书籍详情页获取书籍详情信息
        '''
        from calibre.utils.cleantext import clean_ascii_chars
        from calibre.ebooks.chardet import xml_to_unicode
        import html5lib

        try:
            raw = self.browser.open_novisit(self.url, timeout=self.timeout).read().strip()
        except Exception as e:
            if callable(getattr(e, 'getcode', None)) and \
                    e.getcode() == 404:
                self.log.error('URL malformed: %r'%self.url)
                return
            attr = getattr(e, 'args', [None])
            attr = attr if attr else [None]
            if isinstance(attr[0], socket.timeout):
                msg = '17k.com timed out. Try again later.'
                self.log.error(msg)
            else:
                msg = 'Failed to make details query: %r' %self.url
                self.log.exception(msg)
            return

        oraw = raw
        raw = xml_to_unicode(raw, strip_encoding_pats=True,
                resolve_entities=True)[0]
        if '<title>404 - ' in raw:
            self.log.error('URL malformed: %r'%self.url)
            return

        try:
            root = html5lib.parse(clean_ascii_chars(raw), treebuilder='lxml',
                    namespaceHTMLElements=False)
        except:
            msg = 'Failed to parse 17k.com details page: %r'%self.url
            self.log.exception(msg)
            return

        errmsg = root.xpath('//*[@id="errorMessage"]')
        if errmsg:
            msg = 'Failed to parse 17k.com details page: %r'%self.url
            msg += self.tostring(errmsg, method='text', encoding=unicode).strip()
            self.log.error(msg)
            return

        self.parse_details(oraw, root)

    def parse_details(self, raw, root):
        #解析元数据各字段数据
        #self.log.info("=====")
        try:
            asin = self.parse_asin(root)
        except:
            self.log.exception('Error parsing asin for url: %r'%self.url)
            asin = None
        if self.testing:
            import tempfile, uuid
            with tempfile.NamedTemporaryFile(prefix=(asin or str(uuid.uuid4()))+ '_',
                    suffix='.html', delete=False) as f:
                f.write(raw)
            print ('Downloaded html for', asin, 'saved in', f.name)
        # 分析取得书名
        try:
            title = self.parse_title(root)
        except:
            self.log.exception('Error parsing title for url: %r'%self.url)
            title = None
        #分析取得作者
        try:
            authors = self.parse_authors(root)
        except:
            self.log.exception('Error parsing authors for url: %r'%self.url)
            authors = []

        if not title or not authors or not asin:
            self.log.error('Could not find title/authors/asin for %r'%self.url)
            self.log.error('ASIN: %r Title: %r Authors: %r'%(asin, title,
                authors))
            return
        #以书名，作者为元数据对象mi，用于设置元数据
        mi = Metadata(title, authors)
        #设置Bookid
        idtype = '17k'
        mi.set_identifier(idtype, asin)
        self.k17k_id = asin

        #设备注释（简介）
        try:
            mi.comments = self.parse_comments(root)
        except:
            self.log.exception('Error parsing comments for url: %r'%self.url)
        #设置丛书系列
        try:
            series, series_index = self.parse_series(root)
            if series:
                mi.series, mi.series_index = series, series_index
            elif self.testing:
                mi.series, mi.series_index = 'Dummy series for testing', 1
        except:
            self.log.exception('Error parsing series for url: %r'%self.url)
        #设置标签
        try:
            mi.tags = self.parse_tags(root)
        except:
            self.log.exception('Error parsing tags for url: %r'%self.url)

        #设置最后更新日期
#        try:
#            mi.last_modified = self.parse_last_modified(root)
#        except:
#            self.log.exception('Error parsing last_modified for url: %r'%self.url)
        #设置封面
        try:
            self.cover_url = self.parse_cover(root, raw)
        except:
            self.log.exception('Error parsing cover for url: %r'%self.url)

        mi.has_cover = bool(self.cover_url)
        mi.source_relevance = self.relevance
        mi.languages = [u'中文',]

        if self.k17k_id:
            if self.isbn:
                self.plugin.cache_isbn_to_identifier(self.isbn, self.k17k_id)
            if self.cover_url:
                self.plugin.cache_identifier_to_cover_url(self.k17k_id,
                        self.cover_url)

        self.plugin.clean_downloaded_metadata(mi)

        self.result_queue.put(mi)

    def parse_asin(self, root):
        #解析book ID
        id_list = root.xpath(self.k17k_id_xpath)
        #self.log.info("IDs: %s" %id_list)
        if id_list:
            book_num = id_list[0]
            id_pattern = r'\[书号(\d+)\]'
            book_id = re.findall(id_pattern, book_num)[0]
            #self.log.info("BOOK ID: %s" % book_id)
            return book_id


    def totext(self, elem):
        return self.tostring(elem, encoding=unicode, method='text').strip()

    def parse_title(self, root):
        # 解析书名
        title_name = root.xpath(self.title_xpath)
        #self.log.info("BOOK Name: %s" % title_name)
        if title_name:
            title = title_name[0]
            #title = self.tostring(title_name[0], encoding=unicode, method='text').strip()
            ans = re.sub(r'[(\[].*[)\]]', '', title).strip()
            #self.log.info("Name: %s" % ans)
            return ans

    def parse_authors(self, root):
        #解析作者
        aus = root.xpath(self.author_xpath)
        #self.log.info("AUTHORs: %s" % aus)
        authors = []
        if aus:
            #self.log.info("AUTHOR: %s" %aus[0])
            authors.append(aus[0])
            #author = self.tostring(aus[0],.encoding=unicode, method='text')
        return authors


    def _render_comments(self, desc):
        # 生成注释?
        from calibre.library.comments import sanitize_comments_html


        desc = self.tostring(desc, method='html', encoding=unicode).strip()

        # Encoding bug in 17k.com data U+fffd (replacement char)
        # in some examples it is present in place of '
        desc = desc.replace('\ufffd', "'")
        # remove all attributes from tags
        desc = re.sub(r'<([a-zA-Z0-9]+)\s[^>]+>', r'<\1>', desc)
        # Collapse whitespace
        # desc = re.sub('\n+', '\n', desc)
        # desc = re.sub(' +', ' ', desc)
        # Remove the notice about text referring to out of print editions
        desc = re.sub(r'(?s)<em>--This text ref.*?</em>', '', desc)
        # Remove comments
        desc = re.sub(r'(?s)<!--.*?-->', '', desc)
        return sanitize_comments_html(desc)

    def parse_comments(self, root):
        #解析注释
        ans = ''
        desc = root.xpath(self.comments_xpath)
        if desc:
            ans = desc[0].strip()
            #self.log.info("COMMENTS: %s" % ans.encode(encoding='raw_unicode_escape'))

        return ans

    def parse_series(self, root):
        # 解析丛书系列
#        desc = root.xpath(self.series_xpath)
#        if desc:
#            ans = desc[0].encode()
#            self.log.info("SERIES: %s" %ans)
#            return ans
        ans = (None, None)
        desc = root.xpath(self.series_xpath)
        if desc:
            raw = desc[0].encode(encoding='raw_unicode_escape')
            #self.log.info("SERIES: %s" % raw)
            raw = re.sub(r'\s+', ' ', raw)
            match = self.series_pat.search(raw)
            if match is not None:
                s, i = match.group('series'), float(match.group('index'))
                if s:
                    ans = (s, i)
        return ans

    def parse_tags(self, root):
        #解析标签
        ans = []
        for li in root.xpath(self.tags_xpath):
            ans.append(li)
            #self.log.info("TAG: %s" % li.encode(encoding='raw_unicode_escape'))
        #self.log.info("TAGS: %s" % ans)
        return ans

    def parse_cover(self, root, raw=b""):
        #解析封面下载地址

        import urllib
        #imgs_url = 'http://z2-ec2.images-17k.com.com/images/P/'+self.k17k_id+'.01.MAIN._SCRM_.jpg'
        imgs_url = 'https://cdn.static.17k.com/book/189x272/'+self.k17k_id[-2:]+'/'+self.k17k_id[-4:-2]+'/'+self.k17k_id+'.jpg'

        #self.log.info("COVER: %s" %imgs_url)
        try:
            res = urllib.urlopen(imgs_url)
            code = res.getcode()
            res.close()
        except Exception,e:
            code = 404

        if code == 200:
            return imgs_url

        imgs = root.xpath(self.cover_url_xpath)
        if not imgs:
            pass

        if imgs:
            src = imgs[0].get('src') # https://cdn.static.17k.com/book/189x272/17/64/2476417.jpg-189x272?v=0
            #self.log.info("COVER SRC: %s" %s)
            if src:
                parts = src.split('-')
                #self.log.info("COVER: %s" %parts[0])
                return parts[0]

#    def parse_last_modified(self,root):
#        #解析最后更新日期
#        lm = root.xpath(self.last_modified_xpath)
#        print(lm)
#        #self.log.info("UPDATE:%s" lm[0])
#        lm_pattern = r'更新: (.*)'
#        if lm:
#            update = re.findall(lm_pattern, lm)[0]
#            self.log.info("LAST UPDATE: %s" %update)
#            return update
# }}}