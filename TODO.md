# To do

## Reach the Pi from anywhere with Tailscale

The Apple Health sync link (`http://jarvis.local:8787/...`) only works on the home
Wi-Fi, so the iPhone Shortcut checks the Wi-Fi name first and does nothing away from
home. With Tailscale, the phones and the Pi would be on one private network wherever
they are.

- [ ] Install Tailscale on the Pi (`curl -fsSL https://tailscale.com/install.sh | sh`,
      then `sudo tailscale up`) and on both iPhones, signed in to the same tailnet.
- [ ] Set `HEALTH_BASE_URL` in the Pi's `.env` to the Pi's Tailscale name, e.g.
      `http://jarvis.<tailnet>.ts.net:8787`, and restart the bot. Everyone then asks the
      bot for "set up Apple Health" again to get the new link.
- [ ] In each Shortcut, swap in the new link and remove the home Wi-Fi check.
- [ ] Consider binding the sync server to the LAN and Tailscale interfaces only, instead
      of `0.0.0.0` (`calorie_bot/health.py`).
- [ ] Bonus: `ssh`, `./deploy.sh` and the log commands would work away from home too.
      Update the `PI=` default in `deploy.sh`, or document `PI=shawn@jarvis.<tailnet>.ts.net`.
