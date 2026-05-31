# Kripto Piyasa Analiz Botu

Bu bot al-sat islemi yapmaz. BTC ve secili altcoin sepeti icin piyasa verilerini analiz eder ve Telegram'a rapor gonderir.

> Finansal tavsiye degildir. Bot "al" veya "sat" emri uretmez; sadece analiz ve risk uyarisi verir.

## Ozellikler

- Binance public API ile BTC, Layer 1, Layer 2, AI, RWA ve DePIN sembollerini izler.
- `15m`, `1h`, `4h` zaman dilimlerini analiz eder.
- RSI, EMA 20/50/200, MACD, Bollinger, momentum ve hacim degisimini hesaplar.
- Trend yonunu EMA dizilimi, EMA egimi, MACD ivmesi, hacim ve swing yapisina gore skorlayarak yorumlar.
- 1H beklenti gucluyse `Pozisyon: KÂR BEKLENTİSİ`, trend zayifsa `Pozisyon: ZARAR RİSKİ` yazar.
- Binance taker alis/satis baskisi ve order-book duvarlarini izleyerek fake yukselis / dagitim riskini ayirmaya calisir.
- `CRYPTOQUANT_API_KEY` eklenirse Binance'e BTC/ETH net giris-cikis ve stablecoin rezerv degisimini rapora ekler.
- Spot hesap icin long/short dili kullanmadan momentum ve risk ozeti verir.
- BTC 4H bearish/zayif ise altcoinlerde karar otomatik `BTC 4H ZAYIF - BEKLE` olur.
- BTC 15M/1H/4H zayiflama, 5M/1H para cikisi ve hacimli satis birlesirse `Cokus erken uyari` verir.
- Duzeltme geldiginde para USDT'de mi bekliyor, sektor icinde mi donuyor, baska projeye mi kayiyor ayirmaya calisir.
- Anlik para akisini Binance taker alis-satis farkindan yaklasik USDT net baski olarak yazar; kucuk miktarlari `zayif/cok zayif` diye etiketler.
- Altcoin secimi anlik 5M Binance USDT hacmi + 5M fiyat degisimine gore yapilir; 1H teyit olarak raporda gosterilir.
- Destek/direnc seviyelerini onceki swing high/low uzerinden belirler; son mum kirilim yaptiysa bunu daha net yakalar.
- Riskli durumlarda uyari verir:
  - Asiri RSI
  - Ani hacim artisi
  - EMA20/EMA50 kesişimi
  - Bollinger bant temasi
  - Momentum gucu/zayifligi
  - Doji, Hammer, Engulfing mumlari
  - Buyuk alis/satis izi
  - Fake yukselis ve dagitim riski
- Telegram Bot API ile rapor gonderir.
- Varsayilan olarak her 5 dakikada bir otomatik calisir.
- `.env` dosyasindan `TELEGRAM_BOT_TOKEN` ve `TELEGRAM_CHAT_ID` okur.
- Istersen `.env` icinden maliyet seviyelerini okuyup fiyata uzakligi yazar.
- Hata yakalama ve `bot.log` dosyasina loglama vardir.

## Dosya Yapisi

- `data_fetcher.py`: Binance mum verilerini ceker.
- `indicators.py`: RSI, EMA, MACD ve ortalama hesaplari.
- `analyzer.py`: Trend, destek/direnc ve risk uyarilari.
- `telegram_sender.py`: Telegram mesaj gonderimi.
- `telegram_setup.py`: Bot icin chat id bulma yardimcisi.
- `main.py`: Saatlik calisma dongusu.
- `.env.example`: Ornek Telegram ayarlari.
- `deploy_to_vps.sh`: Botu VPS'e tasima yardimcisi.
- `vps_install_service.sh`: VPS uzerinde 5 dakikalik systemd timer kurar.

## Kurulum

Bu bot ek Python paketi istemez; standart kutuphanelerle calisir.

1. `.env.example` dosyasini `.env` olarak kopyala.

```bash
cp .env.example .env
```

2. `.env` dosyasini doldur.

```bash
TELEGRAM_BOT_TOKEN=bot_token
TELEGRAM_CHAT_ID=chat_id
CRYPTOQUANT_API_KEY=
BTCUSDT_COST=
ETHUSDT_COST=2049
SOLUSDT_COST=
FETUSDT_COST=
RENDERUSDT_COST=
ONDOUSDT_COST=
FILUSDT_COST=
ARBUSDT_COST=
OPUSDT_COST=
```

Chat ID bilmiyorsan:

1. Telegram'da botuna `test` yaz.
2. Su komutu calistir.

```bash
python3 telegram_setup.py
```

3. Cikan `CHAT_ID=...` degerini `.env` icindeki `TELEGRAM_CHAT_ID` alanina yaz.

CryptoQuant kullanacaksan `CRYPTOQUANT_API_KEY` alanina access token ekle. Eklenmezse bot zincir ustu veriyi atlar ve Binance verisiyle calismaya devam eder.

3. Tek sefer test et.

```bash
python3 main.py --once
```

Telegram'a gondermeden sadece terminalde gormek icin:

```bash
python3 main.py --once --no-telegram
```

4. 5 dakikada bir calistir.

```bash
python3 main.py
```

Terminal kapaninca da calismasi icin macOS servisi olarak kur:

```bash
./install_launch_agent.sh
```

Kaldirmak icin:

```bash
./uninstall_launch_agent.sh
```

Farkli aralik icin:

```bash
python3 main.py --interval 1800
```

## VPS'e Tasima

VPS Ubuntu/Debian ise botu Mac kapali olsa bile calistirmak icin:

```bash
./deploy_to_vps.sh root@VPS_IP
```

Kurulum sonunda VPS uzerinde systemd timer aktif olur ve bot her 5 dakikada bir calisir.

Durum kontrolu:

```bash
systemctl status binance-signal-bot.timer --no-pager
```

Log kontrolu:

```bash
journalctl -u binance-signal-bot.service -n 80 --no-pager
```

## GitHub Actions ile 5 Dakikada Bir Calistirma

Mac kapali olsa bile GitHub botu calistirsin istiyorsan repo icinde su workflow hazir:

```text
.github/workflows/binance-signal-bot.yml
```

GitHub'da repo actiktan sonra `Settings > Secrets and variables > Actions > New repository secret`
bolumune sunlari ekle:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Maliyet yazmak istersen opsiyonel olarak bunlari da secret olarak ekleyebilirsin:

```text
BTCUSDT_COST
ETHUSDT_COST
SOLUSDT_COST
FETUSDT_COST
RENDERUSDT_COST
ONDOUSDT_COST
FILUSDT_COST
ARBUSDT_COST
OPUSDT_COST
```

Workflow her 5 dakikada bir calisir. Manuel test icin GitHub'da:

```text
Actions > Binance Signal Bot > Run workflow
```

## Rapor Ornegi

```text
KRIPTO RAPORU
2026-05-31 00:15:00 CEST
Cokus erken uyari ⚠️ BTC 15M zayif | 5M para cikisi -0.3% | hacimli satis
5M Net Akış: Layer 1 +420K$ | küçük (ETH +310K$)
Düzeltme: Para coinlerden çıkıp USDT tarafında bekliyor

BTCUSDT
Sektor: BTC
Fiyat: 73896
Pozisyon: KÂR BEKLENTİSİ 🟢 | 1H yüksek
Zincir: Binance net çıkış -1.2K BTC ✅
İz: Alıcı %56 | OB alış duvarı
Yükseliş sinyali var 🟢
5M Akış: +82K$ zayıf | 1H -0.2%
15M: TOPARLANMA 🟢 | Güç +3 | RSI 44 | ERKEN TAKİP
1H: NÖTR 🟡 | Güç +1 | RSI 55 | İZLE
4H: ZAYIFLAMA 🔴 | Güç -3 | RSI 43 | BEKLE
4H Destek/Direnç: 72512 / 78080
Plan: Giriş 72512-73238 | Hedef 78080 | Risk 71425 altı
Alarm: 15M Mum: Hammer

ETHUSDT
Sektor: Layer 1
Fiyat: 2024
Pozisyon: FAKE RİSKİ ⚠️ | kar güveni düşük
Zincir: Binance net giriş +8.4K ETH ⚠️
İz: Satıcı %57 | Dağıtım riski | Fake YÜKSEK ⚠️
5M Akış: -36K$ zayıf | 1H -0.4%
15M: NÖTR 🟡 | Güç +1 | RSI 44 | ALIM BÖLGESİ
1H: TOPARLANMA 🟢 | Güç +3 | RSI 53 | ERKEN TAKİP
4H: NÖTR 🟡 | Güç +0 | RSI 46 | İZLE
4H Destek/Direnç: 1967 / 2140
Alarm: Yok
```
## Notlar

- Binance public API kullanildigi icin API key gerekmez.
- Telegram icin BotFather'dan bot token alman gerekir.
- Chat ID icin kendi Telegram hesabina veya gruba botu ekleyip chat id kullanmalisin.
- Akış satiri `5M Net Akış`, `1H Akış` veya `24s Akış` diye gelir; miktar kucukse sinyali buyutmemek icin `zayif/cok zayif` yazar.
- `market_bot.py` onceki kisa piyasa yonu ve piyasa akisi yardimci aracidir; yeni moduler botun ana girisi `main.py` dosyasidir.
