import logging
import os
import threading

from gi.repository import GLib

from udfarama.autostart import config_directory
from udfarama.memory import SQLiteBuilder, SQLiteIndex
from udfarama.migration import inspect_legacy, migrate_legacy
from udfarama.search import read_udf_document
from udfarama.watch import TreeWatch


class IndexController:
    def __init__(self, on_ready, on_updated, on_status, on_error):
        self.on_ready = on_ready
        self.on_updated = on_updated
        self.on_status = on_status
        self.on_error = on_error
        self.folder = None
        self.store = None
        self.ready = False
        self._generation = 0
        self._cancel = threading.Event()
        self._watch = None
        self._queued = []
        self._refresh_waiting = None
        self._refresh_busy = False
        self._active = True
        self._watch_failure_count = 0
        self._pending_folder = None
        self._finish_callback = None

    def open_folder(self, path):
        path = os.path.abspath(path)
        if self._watch is not None and self.folder is not None:
            self._watch.flush()
        if self._refresh_busy:
            self._pending_folder = path
            if self._watch is not None:
                self._watch.flush()
                self._watch.clear()
                self._watch = None
            return
        self._pending_folder = None
        self._cancel.set()
        self._generation += 1
        generation = self._generation
        self._cancel = threading.Event()
        if self._watch is not None:
            self._watch.clear()
        self._watch = TreeWatch(self._on_changes, on_failure=self._watch_failure)
        self._watch_failure_count = 0
        self._queued.clear()
        self._refresh_waiting = None
        self._refresh_busy = False
        self.folder = path
        self.store = None
        self.ready = False
        self._watch.watch(path)
        worker = threading.Thread(
            target=self._open_worker,
            args=(path, generation, self._cancel),
            name="udfarama-index", daemon=True,
        )
        worker.start()

    def _open_worker(self, folder, generation, cancel):
        try:
            if SQLiteIndex.available(folder):
                store = SQLiteIndex(folder)
            else:
                legacy = config_directory() / "index.json"
                if legacy.exists():
                    header = inspect_legacy(legacy)
                    same_folder = os.path.abspath(header.get("folder", "")) == folder
                    if same_folder:
                        GLib.idle_add(self._progress, generation, "Eski arama indeksi SQLite'a aktarılıyor…")
                        store = migrate_legacy(
                            legacy, folder, cancel,
                            lambda n: GLib.idle_add(self._progress, generation, f"{n:,} kayıt aktarıldı"),
                            lambda m: GLib.idle_add(self._progress, generation, m),
                        )
                    else:
                        store = self._scan_initial(folder, generation, cancel)
                else:
                    store = self._scan_initial(folder, generation, cancel)
            if not cancel.is_set():
                GLib.idle_add(self._opened, generation, store, None)
        except InterruptedError:
            return
        except Exception as error:
            logging.exception("İndeks açılamadı")
            GLib.idle_add(self._opened, generation, None, str(error))

    def _scan_initial(self, folder, generation, cancel):
        builder = SQLiteBuilder(folder)
        try:
            errors = []
            def onerror(error):
                errors.append(str(error))
            for base, dirs, files in os.walk(folder, followlinks=False, onerror=onerror):
                if cancel.is_set():
                    raise InterruptedError()
                dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
                builder.add_directory(base)
                GLib.idle_add(self._watch_directory, generation, base)
                for name in files:
                    if cancel.is_set():
                        raise InterruptedError()
                    if not name.lower().endswith(".udf"):
                        continue
                    path = os.path.join(base, name)
                    if builder.already_indexed(path):
                        continue
                    doc = read_udf_document(path)
                    if doc is not None:
                        builder.add(doc)
                    if builder.count % 250 == 0:
                        GLib.idle_add(self._progress, generation, f"{builder.count:,} belge indekslendi")
            if errors:
                raise PermissionError(f"{len(errors)} klasör okunamadı: {errors[0]}")
            return builder.finish(cancel, lambda message: GLib.idle_add(self._progress, generation, message))
        except BaseException:
            builder.abort()
            raise

    def _progress(self, generation, message):
        if generation == self._generation and self._active:
            self.on_status(message)
        return GLib.SOURCE_REMOVE

    def _watch_failure(self, path, error):
        self._watch_failure_count += 1
        if self._watch_failure_count <= 3:
            self.on_status("Bazı alt klasörler canlı izlenemiyor: " + str(path))
        logging.warning("İzleme başarısız: %s: %s", path, error)

    def _watch_directory(self, generation, directory):
        if self._active and generation == self._generation and self._watch is not None:
            self._watch.watch(directory)
        return GLib.SOURCE_REMOVE

    def _install_saved_watches(self, generation, store):
        def worker():
            try:
                for chunk in store.directories(chunk=40):
                    if not self._active or generation != self._generation:
                        break
                    GLib.idle_add(self._watch_many, generation, chunk)
            except Exception:
                logging.exception("Klasör izleyicileri kurulamadı")
        threading.Thread(target=worker, name="udfarama-watches", daemon=True).start()

    def _watch_many(self, generation, chunk):
        if self._active and generation == self._generation:
            for path in chunk:
                self._watch_directory(generation, path)
        return GLib.SOURCE_REMOVE

    def _opened(self, generation, store, error):
        if not self._active or generation != self._generation:
            return GLib.SOURCE_REMOVE
        if error:
            self.on_error("İndeks hazırlanamadı: " + error)
            self.on_status("İndeks hatası. Kayıtlı belgeler otomatik yeniden taranmadı.")
            return GLib.SOURCE_REMOVE
        self.store = store
        self.ready = True
        self._install_saved_watches(generation, store)
        self.on_ready()
        if self._queued:
            changed = set()
            removed = set()
            for updates, deletions in self._queued:
                changed.update(updates)
                removed.update(deletions)
            self._queued.clear()
            self._on_changes(changed, removed)
        return GLib.SOURCE_REMOVE

    def _on_changes(self, changed, removed):
        if not self.ready:
            self._queued.append((set(changed), set(removed)))
            return
        if self._refresh_busy:
            if self._refresh_waiting is None:
                self._refresh_waiting = (set(changed), set(removed))
            else:
                self._refresh_waiting[0].update(changed)
                self._refresh_waiting[1].update(removed)
            return
        self._refresh_busy = True
        generation = self._generation
        store = self.store
        threading.Thread(
            target=self._refresh_worker,
            args=(generation, store, set(changed), set(removed), self._cancel),
            name="udfarama-refresh", daemon=True,
        ).start()

    def _refresh_worker(self, generation, store, changed, removed, cancel):
        try:
            count = store.refresh(
                changed, removed, cancel,
                on_directory=lambda d: GLib.idle_add(self._watch_directory, generation, d)
            )
            GLib.idle_add(self._refresh_done, generation, count, None)
        except Exception as error:
            logging.exception("Canlı indeks güncellenemedi")
            GLib.idle_add(self._refresh_done, generation, 0, str(error))

    def _refresh_done(self, generation, count, error):
        if not self._active or generation != self._generation:
            return GLib.SOURCE_REMOVE
        self._refresh_busy = False
        if error:
            self.on_error("Canlı güncelleme başarısız: " + error)
        elif count:
            self.on_updated()
        waiting = self._refresh_waiting
        self._refresh_waiting = None
        if waiting:
            self._on_changes(*waiting)
        elif self._finish_callback is not None:
            self._complete_shutdown()
        elif self._pending_folder is not None:
            self.open_folder(self._pending_folder)
        return GLib.SOURCE_REMOVE

    def finish(self, callback):
        if not self._active:
            callback()
            return
        self._finish_callback = callback
        if self._watch is not None:
            self._watch.flush()
            self._watch.clear()
            self._watch = None
        if not self._refresh_busy:
            if self._refresh_waiting:
                waiting = self._refresh_waiting
                self._refresh_waiting = None
                self._on_changes(*waiting)
            else:
                self._complete_shutdown()

    def _complete_shutdown(self):
        callback = self._finish_callback
        self._finish_callback = None
        self.close()
        if callback is not None:
            callback()

    def close(self):
        if not self._active:
            return
        self._active = False
        self._generation += 1
        self._cancel.set()
        if self._watch is not None:
            self._watch.clear()
            self._watch = None
        self._queued.clear()
        self._refresh_waiting = None
        self._pending_folder = None
