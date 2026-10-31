# Privacy Policy

*[Українською](PRIVACY.md) · English*

**RcloneSync GUI** is a desktop program for personal use that runs locally on
the user's computer.

## What data is processed

The program collects, transmits and stores no data about the user.

The only data it touches is the **user's own files in Google Drive**, and only
when the user starts a transfer themselves. The program calls
[rclone](https://rclone.org), which talks to the Google Drive API on the
user's behalf.

## Where the data goes

Files are transferred **directly between the user's computer and Google
Drive** over a secure connection (HTTPS). No intermediate servers are
involved.

The author of the program has access neither to the files nor to the user's
account.

## Where credentials are stored

The access token is stored by rclone in its own configuration file
(`rclone.conf`) **on the user's computer**. The program never sends it
anywhere and never copies it.

Window settings and transfer parameters are stored locally in a
`settings.json` file.

## What there is none of

* analytics or telemetry
* passing data to third parties
* advertising
* accounts or any server side

## Contact

Questions about this policy: vasiliygts@gmail.com

The source code is open and available for inspection:
https://github.com/vasiliygts/GoogleDrive-rclone-uploader-downloader-GUI
