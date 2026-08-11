# Security and privacy policy

## Project scope

Doop is a method-and-evidence repository for a local-first engineering
showcase. It is not a hosted service, a medical device, or a production health
product. The maintainer reviews reports affecting the default branch and the
most recent tagged showcase release on a best-effort basis, but does not promise
production support, a response-time SLA, or a bug-bounty payment.

Security and privacy reports are welcome when they concern the published code,
test fixtures, documentation, release artifacts, or repository history.

## Report sensitive issues privately

Do not put any of the following in a public issue, pull request, discussion,
commit, screenshot, or log:

- health, workout, sleep, location, or demographic data tied to a person;
- database or export contents;
- credentials, tokens, private keys, chat identifiers, or device identifiers;
- owner-linked dates, local filesystem paths, profile details, or private
  runtime configuration;
- the literal value of data that may already have been exposed.

Use GitHub's **Report a vulnerability** control on the repository's Security
tab when it is available. If private vulnerability reporting is unavailable,
open the public privacy-report template and select **Request a private contact
channel**. Include only a filename, a broad risk category, and a request to
continue privately. Do not include the sensitive value or a reproduction that
contains it.

A useful private report includes:

1. the affected path, commit, release, or feature;
2. the impact and realistic exposure path;
3. a synthetic reproduction, when one can be created safely;
4. whether the issue appears to be actively exploitable or publicly exposed;
5. suggested containment or remediation, if known.

The maintainer may ask for additional information through a private channel.
Please allow a reasonable best-effort review period before publishing details.

## Accidental personal-data exposure

If you find personal data in the repository or its history:

1. stop copying, quoting, or redistributing it;
2. report the affected path and commit privately without repeating the value;
3. do not open a pull request that merely deletes it from the current tree,
   because earlier Git objects may remain reachable;
4. preserve only the minimum metadata needed for the maintainer to investigate.

History remediation can require a clean rebuild or coordinated history rewrite
plus cache removal. A normal follow-up commit is not sufficient.

## Security model and responsible testing

- Use only devices and data that you own or are explicitly authorized to test.
- Keep real databases, exports, packet captures, screenshots, and runtime
  configuration outside the repository.
- Reproduce bugs with generated packets and synthetic records.
- Do not use a report as authorization to probe another person's device,
  account, network, or hosted service.
- Third-party findings must include provenance and license status; do not paste
  unlicensed source code or captured third-party data.

## Medical safety

Recovery, strain, HRV, sleep, and training outputs in this repository are
independent engineering proxies. They are not medical advice, diagnostic
results, vendor-score parity, or ground truth. Do not rely on this project for
emergency, treatment, medication, or injury decisions. Seek an appropriate
qualified professional or emergency service for real health concerns.
