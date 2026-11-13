import logging
import threading

from gi.repository import GLib

from udfarama.memory import PAGE_SIZE
from udfarama.search import should_live_search


LIVE_SEARCH_DELAY_MS = 300


class SearchController:
    def initialize_search(self):
        self.searching = False
        self._has_searched = False
        self._last_search_term = ""
        self._cancel_event = threading.Event()
        self._search_generation = 0
        self._live_search_source = None
        self._search_when_ready = False
        self._next_cursor = 0
        self._has_more = False

    def cancel_search(self):
        self._cancel_event.set()
        self._search_generation += 1
        self.searching = False
        self._set_search_busy(False)

    def _on_search_changed(self, _entry):
        if self._live_search_source is not None:
            GLib.source_remove(self._live_search_source)
            self._live_search_source = None
        query = self.search_entry.get_text().strip()
        if not should_live_search(query):
            self.cancel_search()
            self.model.remove_all()
            self._has_searched = False
            self._has_more = False
            self.more_button.set_visible(False)
            self._update_empty_state()
            self._update_results_label(0)
            self._restore_idle_status()
            return
        self._live_search_source = GLib.timeout_add(
            LIVE_SEARCH_DELAY_MS, self._run_live_search
        )

    def _run_live_search(self):
        self._live_search_source = None
        if should_live_search(self.search_entry.get_text()):
            self.start_search(live=True)
        return GLib.SOURCE_REMOVE

    def start_search(self, *_args, live=False, append=False, quiet=False):
        query = self.search_entry.get_text().strip()
        if not query:
            self.cancel_search()
            if not live:
                self.show_error("Aranacak ifade boş olamaz.")
            return
        if not self._index.ready:
            self._search_when_ready = query
            self._set_status_primary("İndeks hazırlanıyor; arama hazır olunca başlayacak.")
            return
        if self.searching:
            self._cancel_event.set()
        self._search_generation += 1
        generation = self._search_generation
        self._cancel_event = threading.Event()
        self.searching = True
        if not append:
            self.model.remove_all()
            self._next_cursor = 0
            self._has_more = False
        self.more_button.set_visible(False)
        self._last_search_term = query
        self._set_search_busy(True)
        if not quiet and not append:
            self._set_status_primary("Belgeler aranıyor…")
        store = self._index.store
        cursor = self._next_cursor if append else 0
        thread = threading.Thread(
            target=self._search_worker,
            args=(store, query, generation, cursor, append, self._cancel_event),
            name="udfarama-search", daemon=True,
        )
        thread.start()

    def _search_worker(self, store, query, generation, cursor, append, cancel_event):
        try:
            page = store.search(query, after_id=cursor, limit=PAGE_SIZE, cancel_event=cancel_event)
            GLib.idle_add(self._search_done, generation, page, append, None)
        except Exception as error:
            logging.exception("SQLite araması başarısız")
            GLib.idle_add(self._search_done, generation, None, append, str(error))

    def _search_done(self, generation, page, append, error):
        if generation != self._search_generation:
            return GLib.SOURCE_REMOVE
        self.searching = False
        self._set_search_busy(False)
        if error:
            self.show_error("Arama tamamlanamadı: " + error)
            self._set_status_primary("Arama hatası")
            return GLib.SOURCE_REMOVE
        if page is None or page.cancelled:
            return GLib.SOURCE_REMOVE
        for result in page.matches:
            self.model.append(self.result_type.from_match(result))
        self._has_searched = True
        self._has_more = page.has_more
        self._next_cursor = page.cursor
        self.more_button.set_visible(page.has_more)
        self._update_empty_state()
        self._update_results_label(self.model.get_n_items())
        self._restore_idle_status()
        return GLib.SOURCE_REMOVE

    def load_more(self, *_args):
        if self._has_more and not self.searching:
            self.start_search(append=True)

    def on_search_button(self, *_args):
        if self.searching:
            self.cancel_search()
        else:
            self.start_search()

    def _set_search_busy(self, busy):
        self.search_button.set_label("Durdur" if busy else "Ara")
        self.spinner.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def on_index_updated(self):
        if self._has_searched and self.search_entry.get_text().strip() == self._last_search_term:
            self.start_search(quiet=True)
        else:
            self._restore_idle_status()

    def on_index_ready(self):
        query = self._search_when_ready
        self._search_when_ready = False
        self._restore_idle_status()
        if query and self.search_entry.get_text().strip() == query:
            self.start_search()

    def shutdown_search(self):
        self.cancel_search()
        if self._live_search_source is not None:
            GLib.source_remove(self._live_search_source)
            self._live_search_source = None
