# NSI Aggregator Proxy

The NSI Aggregator Proxy offers a REST API to a single, simplified connection
state-machine. This in contrast with the multiple state machines needed to keep
track of the NSI connection state when talking to an NSI Aggregator directly
(like [Safnari](https://github.com/BandwidthOnDemand/nsi-safnari)).  This proxy
allows to reserve, provision, release and terminate a connection, and list all
connections including details.

## Simplified Connection State Machine

In the diagram below, the connection state machine is described. The Reserve,
Provision, Release and Terminate actions map to the API endpoints described
below.

```mermaid
%%{init: {"look": "handDrawn", "theme": "neutral"}}%%
stateDiagram-v2
    [*] --> RESERVING : Reserve
    state RESERVING <<choice>>
    RESERVING --> RESERVED : success
    RESERVING --> FAILED : fail
    FAILED --> TERMINATED : Terminate
    RESERVED --> ACTIVATING : Provision
    state ACTIVATING <<choice>>
    ACTIVATING --> ACTIVATED : succes
    ACTIVATING --> FAILED : fail
    ACTIVATED --> DEACTIVATING : Release
    state DEACTIVATING <<choice>>
    DEACTIVATING --> RESERVED : success
    DEACTIVATING --> FAILED : fail
    RESERVED --> TERMINATED : Terminate
``` 

## API Endpoints

### POST /reservations

Reserve a connection using the parameters from the input payload. When the
request is accepted, the reservations transitions to the `RESERVING` state. The
result of the request will be sent to `callbackURL`, and the reservation will
either transition to the `RESERVED` or the `FAILED` state.

#### Input

All fields are mandatory, except for `globalReservationId` and `serviceType`.

```json
{
  "globalReservationId": "urn:uuid:5fa943ae-32e8-4faa-9080-0bbdc0f405e8",
  "description": "My first multi domain connection",
  "criteria": {
    "serviceType": "http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE",
    "p2ps": {
      "capacity": 1000,
      "sourceSTP": "urn:ogf:network:x.domain.toplevel:2020:topology:ps1?vlan=1790",
      "destSTP": "urn:ogf:network:y.domain.toplevel:2025:topology:ps2?vlan=1790"
    }
  },
  "requesterNSA": "urn:ogf:network:y.domain.topolevel:2021:requester",
  "providerNSA": "urn:ogf:network:nsi.example.domain:2025:nsa:safnari",
  "callbackURL": "https://orchestrator.example.domain/callback"
}
```

#### Response

See [API responses](#api-responses).

#### Internal NSI state machine

```mermaid
%%{init: {"look": "handDrawn", "theme": "neutral"}}%%
stateDiagram-v2
    [*] --> ReserveStart : POST /reservations
    state ReserveStart <<choice>>
    ReserveStart --> ReserveChecking : --> reserve
    state ReserveChecking <<choice>>
    ReserveChecking --> ReserveHeld : <-- reserveConfirmed
    ReserveChecking --> ReserveFailed : <-- reserveFailed
    ReserveHeld --> ReserveCommitting : --> reserveCommit
    state ReserveCommitting <<choice>>
    ReserveHeld --> ReserveTimeout : <-- reserveTimeout
    ReserveCommitting --> ReserveCommitted : <-- reserveCommitConfirmed
    ReserveCommitting --> ReserveCommitFailed : <-- reserveCommitFailed
    ReserveFailed --> [*] :  status FAILED
    ReserveCommitFailed --> [*] :  status FAILED
    ReserveTimeout --> [*] :  status FAILED
    ReserveCommitted --> [*] : status RESERVED

```

### POST /reservations/{connectionId}/provision

Provision a connection identified by connectionId, this is only allowed when
the reservation is in the `RESERVED` state. When the request is accepted, the
reservations transitions to the `ACTIVATING` state. The result of the request
will be sent to `callbackURL`, and the reservation will either transition to
the `ACTIVATED` or the `FAILED` state.

#### Input

```json
{
  "callbackURL": "https://orchestrator.example.domain/callback"
}
```

#### Response

See [API responses](#api-responses).

#### Internal NSI state machine

```mermaid
%%{init: {"look": "handDrawn", "theme": "neutral"}}%%
stateDiagram-v2
    [*] --> ProvisionStart : POST /reservations/{connectionId}/provision
    state ProvisionStart <<choice>>
    ProvisionStart --> Provisioning : --> provision
    state Provisioning <<choice>>
    Provisioning --> Provisioned : <-- provisionConfirmed
    state Provisioned <<choice>>
    Provisioning --> ProvisionFailed : <-- error
    Provisioned --> Activated : <-- dataPlaneStateChange up
    Provisioned --> ActivationTimedOut : timeout
    Activated --> [*] :  status ACTIVATED
    ActivationTimedOut --> [*] :  status FAILED
    ProvisionFailed --> [*] :  status FAILED
```

### POST /reservations/{connectionId}/release

Release a connection identified by conection_id.  this is only allowed when the
reservation is in the `ACTIVATED` state. When the request is accepted, the
reservations transitions to the `DEACTIVATING` state. The result of the request
will be sent to `callbackURL`, and the reservation will either transition to
the `RESERVED` or the `FAILED` state.

#### Input

```json
{
  "callbackURL": "https://orchestrator.example.domain/callback"
}
```

#### Response

See [API responses](#api-responses).

#### Internal NSI state machine

```mermaid
%%{init: {"look": "handDrawn", "theme": "neutral"}}%%
stateDiagram-v2
    [*] --> ReleaseStart : POST /reservations/{connectionId}/release
    state ReleaseStart <<choice>>
    ReleaseStart --> Releasing : --> release
    state Releasing <<choice>>
    Releasing --> Released : <-- releaseConfirmed
    state Released <<choice>>
    Releasing --> ReleaseFailed : <-- error
    Released --> Deactivated : <-- dataPlaneStateChange down
    Released --> DeactivationTimedOut : timeout
    Deactivated --> [*] :  status RESERVED
    DeactivationTimedOut --> [*] :  status FAILED
    ReleaseFailed --> [*] :  status FAILED
```

### DELETE /reservations/{connectionId}

Terminate a reserved connection identified by conection_id.  this is only
allowed when the reservation is in the `RESERVED` or `FAILED` state. When the
request is accepted, the reservations transitions to the `TERMINATED` state.

#### Input

```json
{
  "callbackURL": "https://orchestrator.example.domain/callback"
}
```

#### Response

See [API responses](#api-responses).

#### Internal NSI state machine

```mermaid
%%{init: {"look": "handDrawn", "theme": "neutral"}}%%
stateDiagram-v2
    [*] --> TerminateStart : DELETE /reservations/{connectionId}
    state TerminateStart <<choice>>
    TerminateStart --> Terminating : --> terminate
    state Terminating <<choice>>
    Terminating --> Terminated : <-- terminateConfirmed
    Terminating --> TerminateFailed : <-- error
    Terminated --> [*] :  status TERMINATED
    TerminateFailed --> [*] :  status TERMINATED
```

### GET /reservations/{connectionId}

Get the details of the reservation identified by `connectionId`.

#### Response

```json
  {
    "globalReservationId": "urn:uuid:5fa943ae-32e8-4faa-9080-0bbdc0f405e8"
    "connectionId": "9adfed42-fa58-4d26-bf74-9f5e14ab2281"
    "description": "My first multi domain connection",
    "criteria": {
      "version": 1,
      "serviceType": "http://services.ogf.org/nsi/2013/12/descriptions/EVTS.A-GOLE",
      "p2ps": {
        "capacity": 1000,
        "sourceSTP": "urn:ogf:network:x.domain.toplevel:2020:topology:ps1?vlan=1790",
        "destSTP": "urn:ogf:network:y.domain.toplevel:2025:topology:ps2?vlan=1790"
      }
    },
    "status": "ACTIVATED"
  }
```

### GET /reservations

Get a list of all reservations and details.

#### Response

A list of reservation details as returned by `GET /reservations/{connectionId}`.

```json
{
  "reservations": [
    ....
  ]
}
```

## API Responses

### On Requests

#### 202 Accepted

The request is syntactically correct, passed the initial validation of the
payload, and has been accepted. The result of the request will be send to
`callbackURL`.

```json
{
  "type": "https://github.com/workfloworchestrator/nsi-aggregator-proxy#202-accepted",
  "title": "Accepted",
  "status": 202,
  "detail": "The request is accepted.",
  "instance": "/reservations/9adfed42-fa58-4d26-bf74-9f5e14ab2281"
}
```

#### 400 Bad Request

The JSON is syntactically broken (e.g., a missing comma, unclosed brace, or
invalid characters).

```json
{
  "type": "https://github.com/workfloworchestrator/nsi-aggregator-proxy#202-bad-request",
  "title": "Bad Request",
  "status": 400,
  "detail": "The JSON is syntactically broken.",
  "path": "/reservations"
}
```

#### 415 Unsupported Media Type

Only JSON payload is accepted, set the `Content-Type` header to
`application/json; charset=utf-8`.

```json
{
  "type": "https://github.com/workfloworchestrator/nsi-aggregator-proxy#415-unsupported-media-type",
  "title": "Unsupported Media Type",
  "status": 415,
  "detail": "Only application/json with UTF-8 encoding is supported.",
  "path": "/reservations"
}
```

#### 422 Unprocessable Entity

The payload contains invalid data, for example, the sourceSTP is unknown, or
the capacity is a negative number.

```json
{
  "type": "https://github.com/workfloworchestrator/nsi-aggregator-proxy#422-unprocessable-entity",
  "title": "Unprocessable Entity",
  "status": 422,
  "detail": "The STP cannot be found in any of the know topologies.",
  "instance": "/reservations/5fa943ae",
  "errors": [
    {
      "field": "sourceSTP",
      "reason": "STP 'urn:ogf:network:x.domain.toplevel:2020:topology:ps1?vlan=1790' not found."
    }
  ]
}
```

### Callback Payload

A payload identical to the one returned by `GET /reservations/{connectionId}`.
