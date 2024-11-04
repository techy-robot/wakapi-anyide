# wakapi-anyide
Log your coding time against any WakaTime-like server.

> [!NOTE]
> Teenager? Check out [High Seas](https://highseas.hackclub.com/)!

> [!IMPORTANT]
> wakapi-anyide only supports Linux. This is due to it using inotify.

> [!IMPORTANT]
> wakapi-anyide is still in development.

## Alternatives
You should try use an [editor extension](https://wakatime.com/plugins) over wakapi-anyide if you can.
If you don't need precise coding metrics, use the [WakaTime app](https://wakatime.com/linux) instead. (Not for High Seas!)

## Quickstart guide
- **Set your IDE to autosave as quickly as possible.** This is how wakapi-anyide is able to track your coding time. Ideally, something like 100ms, so it saves as you type.
- Install `wakapi-anyide` with your favourite Python package manager
- Run `wakapi-anyide setup`
- Inspect and edit the generated `wak.toml`:
  ```toml
  # https://github.com/iamawatermelo/wakapi-anyide v0.0.1
  
  [wakapi-anyide]
  version = 1                               # don't change this
  
  [files]
  include = ["./**/*.py", "./**/*.toml"]    # files to include in tracking
  exclude = [".venv/**/*"]                  # files to exclude in tracking
  exclude_files = [".gitignore"]            # files whose contents will be used to exclude other files from tracking
  
  [project]
  name = "wakapi-anyide"                  # your project name
  ```
- Run `wakapi-anyide test` in the same directory you have `wak.toml` in, and start coding for a bit.
  Ensure that wakapi-anyide is not tracking any generated files.
- Run `wakapi-anyide track` to actually track your progress.
  You must run this every time.

## What wakapi-anyide sends
wakapi-anyide tells the WakaTime server:
- that you are using wakapi-anyide
- the relative path of the files you change, e.g "./wakatime_anyide/__init__.py"
- the estimated time you have spent
- your estimated cursor position over time
- the estimated language of any files you edit
- the amount of lines changed
- the branch you are editing if you use Git
- any information you specified in the project section of `wak.toml`

Additionally, the WakaTime server will be able to see:
- your IP address, which means your approximate location
- the time of day your requests are being sent
Every website you visit can see this information.

wakapi-anyide does not send:
- any information about your system, like your username

For security, wakapi-anyide does not use wakatime-cli.

## Support for existing WakaTime configuration
wakapi-anyide supports the `WAKATIME_HOME` and the `WAKATIME_API_KEY` environment variables.
The configuration value takes precedence over the environment variable.

### $WAKATIME_HOME/.wakatime.cfg
Only these configuration values are supported:

#### [settings]
| option            | description                                                                                          | type     | default value                     |
| ----------------- | ---------------------------------------------------------------------------------------------------- | -------- | --------------------------------- |
| api_key           | Your WakaTime API key.                                                                               | _string_ |                                   |
| api_key_vault_cmd | A command to get your api key. Shell syntax is not supported, use `sh -c "<your command>"` for that. | _string_ |                                   |
| api_url           | The WakaTime API base url.                                                                           | _string_ | <https://api.wakatime.com/api/v1> |
| hostname          | Optional name of local machine.                                                                      | _string_ | wakapi-anyide                   |

All other configuration values are silently ignored.

### .wakatime-project
Not supported.

## Limitations
wakapi-anyide is not integrated with your editor. It can only guess what you are doing.