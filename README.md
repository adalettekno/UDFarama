# UDF Belge Arama

UYAP Doküman Editörü `.udf` dosyalarının içeriğinde hızlı arama yapan Linux masaüstü uygulaması.

Tanıtım ve ayrıntılı bilgi: [UDF Belge Arama — Adalet Tekno](https://www.adalettekno.com/yazilim/udf-belge-arama)

## Kurulum

Paket yöneticinizden `git`, `flatpak` ve `flatpak-builder` araçlarını kurun. Ardından terminalde şu komutları sırayla çalıştırın:

```bash
# Kaynak kodu indirin ve proje klasörüne geçin.
git clone https://github.com/adalettekno/com.adalettekno.UdfArama.git
cd com.adalettekno.UdfArama
```
```bash
# Gerekli çalışma ve geliştirme ortamlarını kurun.
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50
```
```bash
# Uygulamayı oluşturun ve kurun.
flatpak-builder --user --install --force-clean build-dir com.adalettekno.UdfArama.yml
```
```bash
# Uygulamayı açın.
flatpak run com.adalettekno.UdfArama
```
Bu komutlardaki GNOME 50 sürümleri, `com.adalettekno.UdfArama.yml` dosyasındaki çalışma ortamı (runtime) ve SDK sürümleriyle aynıdır. `--force-clean`, önceki derlemeden kalan `build-dir` klasörünü temizler.

## Teknik bilgiler

Uygulama GTK4/PyGObject kullanır. Yalnızca seçtiğiniz klasör ve alt klasörleri taranır; tüm dosya sistemine erişim izni istemez.

UDF dosyalarındaki `content.xml` içeriği UTF-8 metin olarak aranır.
