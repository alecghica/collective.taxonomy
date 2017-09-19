# -*- coding: utf-8 -*-

from collective.taxonomy.behavior import TaxonomyBehavior
from collective.taxonomy.interfaces import ITaxonomy
from collective.taxonomy.interfaces import get_lang_code
from collective.taxonomy.vocabulary import Vocabulary

from BTrees.IOBTree import IOBTree
from BTrees.OOBTree import OOBTree
from OFS.SimpleItem import SimpleItem

from persistent.dict import PersistentDict

from plone import api
from plone.behavior.interfaces import IBehavior
from plone.dexterity.fti import DexterityFTIModificationDescription
from plone.dexterity.interfaces import IDexterityFTI
from plone.memoize import ram

from zope.interface import implementer
from zope.lifecycleevent import modified

import generated
import logging

from copy import copy

from collective.taxonomy import PATH_SEPARATOR, COUNT, ORDER

logger = logging.getLogger("collective.taxonomy")


def pop_value(d, compare_value, default=None):
    for key, value in d.items():
        if compare_value == value:
            break
    else:
        return default

    return d.pop(key)


@implementer(ITaxonomy)
class Taxonomy(SimpleItem):
    def __init__(self, name, title, default_language):
        self.data = PersistentDict()
        self.name = name
        self.title = title
        self.default_language = default_language

    @property
    def sm(self):
        return api.portal.get().getSiteManager()

    def __call__(self, context):
        if not self.data:
            return Vocabulary(self.name, {}, {})

        request = getattr(context, "REQUEST", None)
        language = self.getCurrentLanguage(request)
        return self.makeVocabulary(language)

    @property
    @ram.cache(lambda method, self: (self.name, self.data._p_mtime))
    def inverted_data(self):
        inv_data = {}
        for (language, elements) in self.data.items():
            inv_data[language] = {}
            for (path, identifier) in elements.items():
                if path[0] == u'#':
                    continue
                inv_data[language][identifier] = path
        return inv_data

    def getShortName(self):
        return self.name.split('.')[-1]

    def getGeneratedName(self):
        return 'collective.taxonomy.generated.' + self.getShortName()

    def getVocabularyName(self):
        return 'collective.taxonomy.' + self.getShortName()

    def makeVocabulary(self, language):
        data = self.data[language]
        inverted_data = self.inverted_data[language]
        return Vocabulary(self.name, data, inverted_data)

    def getCurrentLanguage(self, request):
        language = get_lang_code()
        if language in self.data:
            return language
        elif self.default_language in self.data:
            return self.default_language
        else:
            # our best guess!
            return self.data.keys()[0]

    def getLanguages(self):
        return tuple(self.data)

    def iterLanguage(self, language=None):
        if language is None:
            language = self.default_language

        vocabulary = self.makeVocabulary(language)

        for path, identifier in vocabulary.iterEntries():
            parent_path = path.rsplit(PATH_SEPARATOR, 1)[0]
            if parent_path:
                parent = vocabulary.getTermByValue(parent_path)
            else:
                parent = None
            yield path, identifier, parent

    def registerBehavior(self, **kwargs):
        new_args = copy(kwargs)

        new_args['name'] = self.getGeneratedName()
        new_args['title'] = self.title
        new_args['description'] = kwargs.get('field_description', u'')
        new_args['field_description'] = new_args['description']

        behavior = TaxonomyBehavior(**new_args)
        self.sm.registerUtility(behavior, IBehavior,
                                name=self.getGeneratedName())

        behavior.addIndex()
        behavior.activateSearchable()

    def cleanupFTI(self):
        """Cleanup the FTIs"""
        generated_name = self.getGeneratedName()
        for (name, fti) in self.sm.getUtilitiesFor(IDexterityFTI):
            if generated_name in fti.behaviors:
                fti.behaviors = [behavior for behavior in
                                 fti.behaviors
                                 if behavior != generated_name]
            modified(fti, DexterityFTIModificationDescription("behaviors", ''))

    def updateBehavior(self, **kwargs):
        behavior_name = self.getGeneratedName()
        short_name = self.getShortName()

        utility = self.sm.queryUtility(IBehavior, name=behavior_name)
        if utility:
            utility.deactivateSearchable()
            utility.activateSearchable()
            if 'field_title' in kwargs:
                utility.title = kwargs.pop('field_title')

            for k, v in kwargs.iteritems():
                setattr(utility, k, v)

        delattr(generated, short_name)

        for (name, fti) in self.sm.getUtilitiesFor(IDexterityFTI):
            if behavior_name in fti.behaviors:
                modified(fti, DexterityFTIModificationDescription("behaviors", ''))

    def unregisterBehavior(self):
        behavior_name = self.getGeneratedName()
        utility = self.sm.queryUtility(IBehavior, name=behavior_name)

        if utility is None:
            return

        self.cleanupFTI()

        utility.removeIndex()
        utility.deactivateSearchable()
        utility.unregisterInterface()

        self.sm.unregisterUtility(utility, IBehavior, name=behavior_name)

    def clean(self):
        self.data.clear()

    def add(self, language, value, key):
        tree = self.data.get(language)
        if tree is None:
            tree = self.data[language] = OOBTree()
        else:
            # Make sure we update the modification time.
            self.data[language] = tree

        update = key in tree
        tree[key] = value

        order = tree.get(ORDER)
        if order is None:
            order = tree[ORDER] = IOBTree()
            count = 0
        else:
            if update:
                pop_value(tree[COUNT], key)

            count = tree[COUNT] + 1

        tree[COUNT] = count
        order[count] = key

    def update(self, language, items, clear=False):
        tree = self.data.setdefault(language, OOBTree())
        if clear:
            tree.clear()

        # Make sure we update the modification time.
        self.data[language] = tree

        order = tree.setdefault(ORDER, IOBTree())
        count = tree.setdefault(COUNT, 0)

        # The following structure is used to expunge updated entries.
        inv = {}
        if not clear:
            for i, key in order.items():
                inv[key] = i

        seen = set()
        for key, value in items:
            if key in seen:
                logger.warning("Duplicate key entry: %r" % (key, ))

            seen.add(key)
            update = key in tree
            tree[key] = value
            order[count] = key
            count += 1

            # If we're updating, then we have to pop out the old ordering
            # information in order to maintain relative ordering of new items.
            if update:
                i = inv.get(key)
                if i is not None:
                    del order[i]

        tree[COUNT] = count

    def translate(self, msgid, mapping=None, context=None,
                  target_language=None, default=None):

        if target_language is None or \
                target_language not in self.inverted_data:
            target_language = str(self.getCurrentLanguage(
                getattr(context, 'REQUEST')
            ))

        if msgid not in self.inverted_data[target_language]:
            return ''

        path = self.inverted_data[target_language][msgid]
        pretty_path = path[1:].replace(PATH_SEPARATOR, u' » ')

        return pretty_path
