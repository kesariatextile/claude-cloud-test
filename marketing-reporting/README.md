# Marketing Reporting & Dashboard (Google Sheets)

Kesaria Textile marketing team ke liye: har person ki alag Google Sheet + ek master Dashboard.

## Setup (one time, ~5 min)
1. Google Drive me ek **blank Google Sheet** banao, naam: `Marketing Dashboard`.
2. `Extensions → Apps Script` kholo, `Code.gs` ka poora content paste karo, Save.
3. Sheet reload karo → menu **Reporting → 1. Setup everything**. Google permission maangega (Drive + Sheets) — Allow karo.
4. 20 person sheets ek Drive folder `Marketing Reporting - Kesaria Textile` me ban jayengi. Links `Config` tab me milenge.
   - Agar 6 minute limit ki wajah se kuch sheets reh jayein, **Reporting → Create missing person sheets** dobara chalao.
5. Har person ko apni sheet ka link do (share as Editor). Chaho to `Code.gs` ke `TEAMS` me `email` bhar do, sheet auto-share ho jayegi.

## Person sheet
- **Daily Log**: roz ek row per entry — Date, Category (dropdown), Qty (blank = 1), Title, Link. YouTube SEO me Channel (CH1, CH2…) bhi dropdown me.
- **Performance Log** (sirf YouTube SEO): Date, Channel, Video Link, New/Old Video, Views, Comments, Subscribers.
- **Summary**: Daily / Weekly / Monthly / Last Month / Last 6 Months / Yearly — category-wise (aur YT me channel-wise + New vs Old views/comments/subs) auto-calculate.

## Master dashboard
- **Dashboard**: team overview, har team me person-wise aur category-wise, saare 6 periods + charts.
- **YT Dashboard**: period dropdown; channel-wise Long/Short/Community count aur New vs Old performance.
- **All_Data / YT_Performance**: sabka consolidated data (filter ke saath, video links yahin milenge).
- Data **har ghante auto-sync** hota hai. Turant chahiye to **Reporting → Sync data now**.

## Notes
- Arun & Yagna ki 2 sheets hongi (YouTube SEO aur Website SEO) — dono teams me count alag rahega.
- Weekly = Monday se aaj tak, Monthly = mahine ki 1 tareekh se aaj tak, Last 6 Months = aaj se 6 mahine peeche, Yearly = 1 Jan se aaj tak. `PERIOD_START/PERIOD_END` badal kar kuch bhi change kar sakte ho.
- Performance Log me Views/Comments/Subs wo number daalo jo us date (ya week/month) ke liye record karna hai; dashboard unka sum dikhata hai.
