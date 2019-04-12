# osiris-build-observer

[OpenShift] namespace event observer that accompanies [Osiris API] and triggers relevant endpoints on build events.

<br>

### About

The build observer watches for **build events** in the namespace. When a build event occurs, it notifies [Osiris API] that the build has started, same for the completed builds. There is a plan to incorporate custom hooks in the observer, but it is not possible at the moment.

The build observer triggers for both successful and failed builds. There is an optional configuration to gather the build logs directly. The observer then sends them over to the [Osiris API] which in turn stores them to the Ceph storage.

<br>

### Before deployment

The observer assumes that a **service account** `analyzer` is present in the namespace and has required priviliges to watch namespace events and logs. Refer to the [OpenShift] documentation for more information on how to set up the service account.

<br>

### Steps to deploy

There is an [OpenShift] template [`template.yaml`](/openshift/template.yaml) in the [`openshift`](/openshift/) folder which encapsulates most of the necessary steps to deploy.

To deploy it into your namespace, log in using the `oc login` command and issue

```bash
oc process -f openshift/template.yaml --param OSIRIS_HOST_URL="$OSIRIS_HOST_URL" | oc replace -f -
```

The `OSIRIS_HOST_URL` is the route to the [Osiris API]. The observer will trigger appropriate endpoints on this url.

<br>


Refer to the [Osiris API] for more information.

[OpenShift]: https://www.openshift.com/
[Osiris API]: https://github.com/thoth-station/osiris
