// The build flavour, read by main.js. A dev build also registers the handlers that test Premiere by changing it
// (api_smoke, run by tests/live/api_smoke.py). The maintainers' working tree is a dev build; release exports and
// .ccx packages carry the release flag. To run api_smoke from a release copy, set the flag to true, then Reload.
module.exports = { dev: false };
