# Kurulum

Sürüm: UDFarama 1.0.0

`UDFarama.deb` dosyasını yazılım kurucusuyla açın. Kurucu açmazsa, dosyanın bulunduğu dizinde:

```sh
sudo apt install ./UDFarama_1.0.0_amd64.deb
```

Kurulum bitince UDFarama uygulama menüsünde görünür. Masaüstüne kısayol konmaz.

Uygulama kimliği `com.adalettekno.UDFarama` (masaüstü/metainfo); apt paketi ve komut `udfarama` (küçük harf, Debian kalıbı).

Gereksinimler paket bağımlılıklarıyla gelir: Python 3, PyGObject, GTK 4 ve libadwaita.

Paket Debian ve Ubuntu tabanlı sistemler içindir. amd64 paketi 64 bit Intel ve AMD içindir. ARM makinede o paketi kurmayın.

# Kaldırma

Önce uygulamayı kapatın.

```sh
sudo apt remove udfarama
```

Kayıtlı arama klasörü yolu (`~/.config/com.adalettekno.UDFarama/`) paketlenmez; isterseniz elle silebilirsiniz.

# Derleme

`dpkg-deb` kurulu olsun. Depo kökünde:

```sh
python3 tools/build_deb.py
```

Paket `dist/amd64/UDFarama.deb` olarak yazılır. Mimari farklıysa klasörün adı değişir. Derleme komutu paketi kurmaz.

# Geliştirme kısayolu

Paket kurmadan menü girişi için (depo kökünde):

```sh
python3 tools/install_desktop.py
```

Bu, kaynak ağacını `PYTHONPATH` ile açan bir kullanıcı menü kısayolu yazar.
