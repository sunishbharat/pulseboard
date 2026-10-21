Corporate CA certificates for deploy/Dockerfile.corp (ADDENDUM-01 §5.3).

Leave this folder empty unless the curl diagnostic from inside Cloud Foundry shows a
certificate verify error:

    cf ssh <app> -c "curl -sv --http1.1 https://<jira-host>/rest/api/2/field"

If it does, copy the corporate root certificate(s) here as *.crt (PEM) on the build machine
only. They are git-ignored and must never be committed to the public repository.
