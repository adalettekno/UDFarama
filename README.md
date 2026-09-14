# UDF Belge Arama

UYAP Doküman Editörü `.udf` dosyalarının içeriğinde hızlı arama yapan Linux masaüstü uygulaması.

Tanıtım ve ayrıntılı bilgi: [UDF Belge Arama — Adalet Tekno](https://www.adalettekno.com/yazilim/udf-belge-arama)

## Kurulum

Uygulama Flatpak paketi olarak kurulur. Şu anda Flathub mağazasında yer almıyor. Aşağıdaki komutlar Flathub'dan yalnızca uygulamanın çalışması için gereken yardımcı bileşenleri indirir.

### Hazır dosyadan kurulum

Bilgisayarınızda Flatpak kurulu olmalı ve internet bağlantınız bulunmalıdır. Flatpak kurulu değilse kullandığınız Linux dağıtımının yazılım yöneticisinden kurabilirsiniz.

**1. Kurulum dosyasını indirin.**

Şimdilik yalnızca deneme paketi bulunmaktadır. İndirmek için GitHub hesabınızla giriş yapın:

- [Paketlerin hazırlandığı sayfayı açın.](https://github.com/adalettekno/com.adalettekno.UdfArama/actions/workflows/flatpak.yml)
- `main` yazan, yeşil onay işaretli en son çalışmaya tıklayın.
- Sayfanın altındaki **Artifacts** bölümünden **UdfArama-test-bundle** dosyasını indirin.
- İnen arşivi açıp içindeki **UdfArama.flatpak** dosyasını çıkarın.

İndirme dosyası görünmüyorsa paket artık erişilebilir olmayabilir. Bu durumda aşağıdaki kaynak koddan kurulum yöntemi kullanılabilir.

**2. Uygulamayı kurun.**

`UdfArama.flatpak` dosyasının bulunduğu klasörü açın. Boş bir alana sağ tıklayıp **Terminalde Aç** seçeneğini seçin. Aşağıdaki komutları sırayla çalıştırın; onay sorulursa kabul edin:

```bash
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub org.gnome.Platform//50
flatpak install --user ./UdfArama.flatpak
```

**3. Uygulamayı açın.**

Uygulama menüsünden **UDF Belge Arama**'yı açın. İsterseniz terminalden de başlatabilirsiniz:

```bash
flatpak run com.adalettekno.UdfArama
```

### Geliştiriciler için kaynak koddan kurulum

Bu bölüm, uygulamayı kaynak koddan oluşturmak isteyenler içindir. Hazır dosyadan kurduysanız bu adımlara gerek yoktur.

Paket yöneticinizden `git`, `flatpak` ve `flatpak-builder` araçlarını kurun. Ardından terminalde şu komutları sırayla çalıştırın:

```bash
# Kaynak kodu indirin ve proje klasörüne geçin.
git clone https://github.com/adalettekno/com.adalettekno.UdfArama.git
cd com.adalettekno.UdfArama

# Gerekli çalışma ve geliştirme ortamlarını kurun.
flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub org.gnome.Platform//50 org.gnome.Sdk//50

# Uygulamayı oluşturun ve kurun.
flatpak-builder --user --install --force-clean build-dir com.adalettekno.UdfArama.yml

# Uygulamayı açın.
flatpak run com.adalettekno.UdfArama
```

Bu komutlardaki GNOME 50 sürümleri, `com.adalettekno.UdfArama.yml` dosyasındaki çalışma ortamı (runtime) ve SDK sürümleriyle aynıdır. `--force-clean`, önceki derlemeden kalan `build-dir` klasörünü temizler.

## Teknik bilgiler

Uygulama GTK4/PyGObject kullanır. Klasör seçimi masaüstünün dosya seçicisi üzerinden yapılır; tüm dosya sistemine erişim izni istemez.

UDF dosyaları ZIP kapsayıcısı olarak açılır ve `content.xml` içeriği UTF-8 metin olarak aranır.
