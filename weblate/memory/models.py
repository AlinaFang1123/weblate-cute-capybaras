# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import math
import os
from functools import reduce

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.encoding import force_str
from django.utils.translation import gettext, pgettext
from jsonschema import validate
from jsonschema.exceptions import ValidationError
from translate.misc.xml_helpers import getXMLlang, getXMLspace
from translate.storage.tmx import tmxfile
from weblate_schemas import load_schema

from weblate.lang.models import Language
from weblate.memory.utils import (
    CATEGORY_FILE,
    CATEGORY_PRIVATE_OFFSET,
    CATEGORY_SHARED,
    CATEGORY_USER_OFFSET,
    is_valid_entry,
)
from weblate.utils.db import adjust_similarity_threshold
from weblate.utils.errors import report_error


class MemoryImportError(Exception):
    pass


def get_node_data(unit, node):
    """Generic implementation of LISAUnit.gettarget."""
    # The language should be present as xml:lang, but in some
    # cases it's there only as lang
    return (
        getXMLlang(node) or node.get("lang"),
        unit.getNodeText(node, getXMLspace(unit.xmlelement, "preserve")),
    )


class MemoryQuerySet(models.QuerySet):
    def filter_type(self, user=None, project=None, use_shared=False, from_file=False):
        base = self
        if "memory_db" in settings.DATABASES:
            base = base.using("memory_db")
        query = []
        if from_file:
            query.append(Q(from_file=from_file))
        if use_shared:
            query.append(Q(shared=use_shared))
        if project:
            query.append(Q(project=project))
        if user:
            query.append(Q(user=user))
        return base.filter(reduce(lambda x, y: x | y, query))

    @staticmethod
    def threshold_to_similarity(text: str, threshold: int) -> float:
        """
        Convert machinery threshold into PostgreSQL similarity threshold.

        Machinery threshold typical values:

        - 75 machinery
        - 80 automatic translation (default value)
        - 10 search

        PostgreSQL similarity threshold needs to be higher to avoid too slow
        queries.
        """
        length = len(text)

        base = 0.172489 * math.log(threshold) + 0.207051
        bonus = 7.03436 * math.exp(-6.07957 * base)
        length_bonus = (0.0733418 * math.log(length) + 0.324497) * bonus

        return max(0.6, min(1.0, round(base + length_bonus, 3)))

    def lookup(
        self,
        source_language,
        target_language,
        text: str,
        user,
        project,
        use_shared,
        threshold: int = 75,
    ):
        # Adjust similarity based on string length to get more relevant matches
        # for long strings
        adjust_similarity_threshold(self.threshold_to_similarity(text, threshold))

        # Actual database query
        return (
            self.prefetch_project()
            .filter_type(
                # Type filtering
                user=user,
                project=project,
                use_shared=use_shared,
                from_file=True,
            )
            .filter(
                # Full-text search on source
                source__search=text,
                # Language filtering
                source_language=source_language,
                target_language=target_language,
            )[:50]
        )

    def prefetch_lang(self):
        return self.prefetch_related("source_language", "target_language")

    def prefetch_project(self):
        return self.select_related("project")


class MemoryManager(models.Manager):
    def import_file(self, request, fileobj, langmap=None, **kwargs):
        origin = os.path.basename(fileobj.name).lower()
        name, extension = os.path.splitext(origin)
        if len(name) > 25:
            origin = f"{name[:25]}...{extension}"

        if extension == ".tmx":
            result = self.import_tmx(request, fileobj, origin, langmap, **kwargs)
        elif extension == ".json":
            result = self.import_json(request, fileobj, origin, **kwargs)
        else:
            raise MemoryImportError(gettext("Unsupported file!"))
        if not result:
            raise MemoryImportError(
                gettext("No valid entries found in the uploaded file!")
            )
        return result

    def import_json(self, request, fileobj, origin=None, **kwargs):
        content = fileobj.read()
        try:
            data = json.loads(force_str(content))
        except ValueError as error:
            report_error(cause="Could not parse memory")
            raise MemoryImportError(gettext("Could not parse JSON file: %s") % error)
        try:
            validate(data, load_schema("weblate-memory.schema.json"))
        except ValidationError as error:
            report_error(cause="Could not validate memory")
            raise MemoryImportError(gettext("Could not parse JSON file: %s") % error)
        found = 0
        lang_cache = {}
        for entry in data:
            try:
                self.update_entry(
                    source_language=Language.objects.get_by_code(
                        entry["source_language"], lang_cache
                    ),
                    target_language=Language.objects.get_by_code(
                        entry["target_language"], lang_cache
                    ),
                    source=entry["source"],
                    target=entry["target"],
                    origin=origin,
                    **kwargs,
                )
                found += 1
            except Language.DoesNotExist:
                continue
        return found

    def import_tmx(self, request, fileobj, origin=None, langmap=None, **kwargs):
        if not kwargs:
            kwargs = {"from_file": True}
        try:
            storage = tmxfile.parsefile(fileobj)
        except (SyntaxError, AssertionError):
            report_error(cause="Could not parse")
            raise MemoryImportError(gettext("Could not parse TMX file!"))
        header = next(
            storage.document.getroot().iterchildren(storage.namespaced("header"))
        )
        lang_cache = {}
        srclang = header.get("srclang")
        if not srclang:
            raise MemoryImportError(
                gettext("Source language not defined in the TMX file!")
            )
        try:
            source_language = Language.objects.get_by_code(srclang, lang_cache, langmap)
        except Language.DoesNotExist:
            raise MemoryImportError(gettext("Could not find language %s!") % srclang)

        found = 0
        for unit in storage.units:
            # Parse translations (translate-toolkit does not care about
            # languages here, it just picks first and second XML elements)
            translations = {}
            for node in unit.getlanguageNodes():
                lang_code, text = get_node_data(unit, node)
                if not lang_code or not text:
                    continue
                try:
                    language = Language.objects.get_by_code(
                        lang_code, lang_cache, langmap
                    )
                except Language.DoesNotExist:
                    raise MemoryImportError(
                        gettext("Could not find language %s!") % header.get("srclang")
                    )
                translations[language.code] = text

            try:
                source = translations.pop(source_language.code)
            except KeyError:
                # Skip if source language is not present
                continue

            for lang, text in translations.items():
                self.update_entry(
                    source_language=source_language,
                    target_language=Language.objects.get_by_code(
                        lang, lang_cache, langmap
                    ),
                    source=source,
                    target=text,
                    origin=origin,
                    **kwargs,
                )
                found += 1
        return found

    def update_entry(self, **kwargs):
        if not is_valid_entry(**kwargs):
            return
        if not self.filter(**kwargs).exists():
            self.create(**kwargs)


class Memory(models.Model):
    source_language = models.ForeignKey(
        "lang.Language",
        on_delete=models.deletion.CASCADE,
        related_name="memory_source_set",
    )
    target_language = models.ForeignKey(
        "lang.Language",
        on_delete=models.deletion.CASCADE,
        related_name="memory_target_set",
    )
    source = models.TextField()
    target = models.TextField()
    origin = models.TextField()
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.deletion.CASCADE,
        null=True,
        blank=True,
        default=None,
    )
    project = models.ForeignKey(
        "trans.Project",
        on_delete=models.deletion.CASCADE,
        null=True,
        blank=True,
        default=None,
    )
    from_file = models.BooleanField(default=False)
    shared = models.BooleanField(default=False)

    objects = MemoryManager.from_queryset(MemoryQuerySet)()

    class Meta:
        verbose_name = "Translation memory entry"
        verbose_name_plural = "Translation memory entries"

    def __str__(self):
        return f"Memory: {self.source_language}:{self.target_language}"

    def get_origin_display(self):
        if self.project:
            text = pgettext("Translation memory category", "Project: {}")
        elif self.user:
            text = pgettext("Translation memory category", "Personal: {}")
        elif self.shared:
            text = pgettext("Translation memory category", "Shared: {}")
        elif self.from_file:
            text = pgettext("Translation memory category", "File: {}")
        else:
            text = "Unknown: {}"
        return text.format(self.origin)

    def get_category(self):
        if self.from_file:
            return CATEGORY_FILE
        if self.shared:
            return CATEGORY_SHARED
        if self.project_id:
            return CATEGORY_PRIVATE_OFFSET + self.project_id
        if self.user_id:
            return CATEGORY_USER_OFFSET + self.user_id
        return 0

    def as_dict(self):
        """Convert to dict suitable for JSON export."""
        return {
            "source": self.source,
            "target": self.target,
            "source_language": self.source_language.code,
            "target_language": self.target_language.code,
            "origin": self.origin,
            "category": self.get_category(),
        }
