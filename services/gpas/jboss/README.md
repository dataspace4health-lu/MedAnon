### Sense and Purpose ###
This directory is exclusively for cli-files to initialize the WildFly.<br>
Files with the extension `.cli` are executed each time WildFly is started, whereby each file is only executed once.<br>
In this way, for example, `datasources`, `logger` or `deployment-overlays` can be created.<br>
For further details see here: https://access.redhat.com/documentation/en-us/red_hat_jboss_enterprise_application_platform/7.2/html/management_cli_guide/how_to_cli

### Additional knowledge ###
Additional file-endings can be added for execution via a filter in the form of the ENV variable `CLI_FILTER`.<br>
Files with the extension `.cli` are still taken into account even if this filter is set.

### License ###
**License:** AGPLv3, https://www.gnu.org/licenses/agpl-3.0.en.html<br>
**Copyright:** 2014 - 2025 University Medicine Greifswald<br>
**Contact:** https://www.ths-greifswald.de/kontakt/
