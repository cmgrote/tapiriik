from tapiriik.settings import WEB_ROOT, DAILYMILE_CLIENT_SECRET, DAILYMILE_CLIENT_ID, DAILYMILE_RATE_LIMITS
from tapiriik.services.service_base import ServiceAuthenticationType, ServiceBase
from tapiriik.services.service_record import ServiceRecord
from tapiriik.database import cachedb
from tapiriik.services.interchange import UploadedActivity, ActivityType, ActivityStatistic, ActivityStatisticUnit, Waypoint, WaypointType, Location, Lap
from tapiriik.services.api import APIException, UserException, UserExceptionType, APIExcludeActivity
from tapiriik.services.gpx import GPXIO

from django.core.urlresolvers import reverse
from datetime import datetime, timedelta
from urllib.parse import urlencode
import calendar
import requests
import os
import logging
import pytz
import re
import time
import json

logger = logging.getLogger(__name__)

# NOTE: majority of this service just lifted from Strava service, and then modified where necessary!
# Dailymile API: http://www.dailymile.com/api

class DailymileService(ServiceBase):
    ID = "dailymile"
    DisplayName = "Dailymile"
    DisplayAbbreviation = "DMI"
    AuthenticationType = ServiceAuthenticationType.OAuth
    UserProfileURL = "http://www.dailymile.com/people/{0}"
    AuthenticationNoFrame = True  # They don't prevent the iframe, it just looks really ugly.
    LastUpload = None

    SupportsHR = True

    # For mapping common->Dailymile; no ambiguity in Dailymile activity type
    # NOTE: API documentation quotes these all in lowercase, but API response shows title case
    _activityTypeMappings = {
        ActivityType.Cycling: "Cycling",
        ActivityType.MountainBiking: "Cycling",
        ActivityType.Hiking: "Walking",
        ActivityType.Running: "Running",
        ActivityType.Walking: "Walking",
        ActivityType.Snowboarding: "Fitness",
        ActivityType.Skating: "Fitness",
        ActivityType.CrossCountrySkiing: "Fitness",
        ActivityType.DownhillSkiing: "Fitness",
        ActivityType.Swimming: "Swimming",
        ActivityType.Gym: "Fitness",
        ActivityType.Rowing: "Fitness",
        ActivityType.Elliptical: "Fitness"
    }

    # For mapping Dailymile->common
    _reverseActivityTypeMappings = {
        "Cycling": ActivityType.Cycling,
        "Running": ActivityType.Running,
        "Walking": ActivityType.Walking,
        "Swimming": ActivityType.Swimming,
        "Fitness": ActivityType.Gym,
    }

    SupportedActivities = list(_activityTypeMappings.keys())

    GlobalRateLimits = DAILYMILE_RATE_LIMITS

    def WebInit(self):
        params = {'scope':'write view_private',
                  'client_id':DAILYMILE_CLIENT_ID,
                  'response_type':'code',
                  'redirect_uri':WEB_ROOT + reverse("oauth_return", kwargs={"service": "dailymile"})}
        self.UserAuthorizationURL = \
           "https://api.dailymile.com/oauth/authorize?" + urlencode(params)

    def _apiHeaders(self, serviceRecord):
        return {"Authorization": "access_token " + serviceRecord.Authorization["oauth_token"]}

    def _paramsIncludingAuth(self, params, serviceRecord):
        params["oauth_token"] = serviceRecord.Authorization["oauth_token"]
        return params

    # Yes, this is pathetic, but my Python is this rusty!
    def _getDateFmt(self):
        return "%Y-%m-%dT%H:%M:%SZ"

    def RetrieveAuthorizationToken(self, req, level):
        code = req.GET.get("code")
        params = {"grant_type": "authorization_code", "code": code, "client_id": DAILYMILE_CLIENT_ID, "client_secret": DAILYMILE_CLIENT_SECRET, "redirect_uri": WEB_ROOT + reverse("oauth_return", kwargs={"service": "dailymile"})}

        self._globalRateLimit()
        response = requests.post("https://api.dailymile.com/oauth/token", data=params)
        if response.status_code != 200:
            raise APIException("Invalid code")
        data = response.json()

        authorizationData = {"oauth_token": data["access_token"]}
        # Retrieve the user ID, meh.
        self._globalRateLimit()
        id_resp = requests.get("https://api.dailymile.com/people/me.json", data=authorizationData)
        if response.status_code != 200:
            raise APIException("Unable to retrieve username: " + str(response.status_code))
        return (id_resp.json()["username"], authorizationData)

    def RevokeAuthorization(self, serviceRecord):
        #  you can't revoke the tokens dailymile distributes :\
        pass

    # NOTE: Dailymile API for retrieving activities appears very limited (no GPS data, etc)
    def DownloadActivityList(self, svcRecord, exhaustive=False):
        activities = []
        exclusions = []
        before = earliestDate = None

        while True:
            if before is not None and before < 0:
                break # Caused by activities that "happened" before the epoch. We generally don't care about those activities...
            logger.debug("Req with before=" + str(before) + "/" + str(earliestDate))
            self._globalRateLimit()
            resp = requests.get("https://api.dailymile.com/people/" + str(svcRecord.ExternalID) + "/entries.json", headers=self._apiHeaders(svcRecord), params={"until": before})
            if resp.status_code == 401:
                raise APIException("No authorization to retrieve activity list", block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))

            earliestDate = None

            reqdata = resp.json()

            if not len(reqdata):
                break  # No more activities to see
            elif not len(reqdata["entries"]):
                break  # Same as above -- could have sworn I once saw just an empty array, with no 'entries', so leaving both

            for ride in reqdata["entries"]:
                
                # Because Dailymile can also have media entries (just photos, no workout data), we need to ensure
                # this is actually some kind of fitness activity before we proceed processing the data
                if ('workout' in ride):
                
                    activity = UploadedActivity()
                    # From the rest of the API, looks like Dailymile probably standardizes on UTC (creating new entries expects them to use UTC, and no timezone otherwise available from downloading activities)
                    activity.TZ = pytz.UTC
                    # Pretty sure what's recorded on Dailymile is the completion time (time of posting)
                    activity.EndTime = pytz.utc.localize(datetime.strptime(ride["at"], self._getDateFmt()))
                    if ('title' in ride["workout"]):
                        logger.debug("\tActivity e/t %s: %s" % (activity.EndTime, ride["workout"]["title"]))
                    else:
                        logger.debug("\tActivity e/t %s: %s" % (activity.EndTime, "Untitled"))
                    if not earliestDate or activity.EndTime < earliestDate:
                        earliestDate = activity.EndTime
                        # This is ugly, but unfortunately Dailymile uses a <= comparison, not a strictly < comparison, so without this we risk an infinite loop
                        # (and if we just subtract 1 second we risk skipping entries that all have the same completion time but happen to span multiple pages)
                        # [though if there are ~20 entries all with the same completion time, they'll probably be skipped anyway]
                        before2 = calendar.timegm(activity.EndTime.astimezone(pytz.utc).timetuple())
                        if before2 == before:
                            before = (before2 - 1)
                        else:
                            before = before2
    
                    # Duration isn't mandatory on Dailymile, and may be missing
                    if ('duration' in ride["workout"]):
                        # Hope 'timedelta' assumes we just send it an elapsed time / duration (in seconds) of the activity (?)
                        activity.StartTime = activity.EndTime - timedelta(0, ride["workout"]["duration"])
                    else:
                        activity.StartTime = activity.EndTime # but since other bits expect both to be set, we'll make a default duration of 0

                    activity.ServiceData = {"ActivityID": ride["id"]}

                    if ride["workout"]["activity_type"] not in self._reverseActivityTypeMappings:
                        exclusions.append(APIExcludeActivity("Unsupported activity type %s" % ride["workout"]["activity_type"], activity_id=ride["id"], user_exception=UserException(UserExceptionType.Other)))
                        logger.debug("\t\tUnknown activity")
                        continue

                    activity.Type = self._reverseActivityTypeMappings[ride["workout"]["activity_type"]]

                    # Don't think distance is mandatory on Dailymile, either...
                    if ('distance' in ride["workout"]):
                        # Distance returned is as the user entered it...
                        distunit = ride["workout"]["distance"]["units"]
                        distmeas = ride["workout"]["distance"]["value"]

                        if (distunit == "miles"):
                            activity.Stats.Distance = ActivityStatistic(ActivityStatisticUnit.Miles, value=distmeas)
                        elif (distunit == "yards"):
                            activity.Stats.Distance = ActivityStatistic(ActivityStatisticUnit.Miles, value=(distmeas/1760))
                        elif (distunit == "meters"):
                            activity.Stats.Distance = ActivityStatistic(ActivityStatisticUnit.Meters, value=distmeas)
                        elif (distunit == "kilometers"):
                            activity.Stats.Distance = ActivityStatistic(ActivityStatisticUnit.Kilometers, value=distmeas)
                        else:
                            exclusions.append(APIExcludeActivity("Unsupported distance unit %s" % distunit, activity_id=ride["id"], user_exception=UserException(UserException.Other)))
                            logger.debug("\t\tUnknown measurement unit")
                            continue

                    # Dailymile automatically calculates average speed (and calories), and doesn't seem to support max speed, moving time, or "timer time"
                    # and while they let you enter avg and max heart rates, and upload GPS tracks, the API does not expose them on existing activities

                    # Even title is not mandatory for a workout
                    if ('title' in ride["workout"]):
                        activity.Name = ride["workout"]["title"]
                    elif ('message' in ride):
                        activity.Name = ride["message"]  # assuming people might enter into Message instead of title (?)
                    else:
                        activity.Name = activity.Type  # otherwise resort to just using the activity type (one of the only things that is mandatory)
                    activity.Private = False
                    activity.GPS = False
                    activity.AdjustTZ()
                    activity.CalculateUID()
                    activities.append(activity)

            if not exhaustive or not earliestDate:
                break

        return activities, exclusions

    # TODO: For now, information retrieved will be very basic (no GPS, etc)
    # URL included below to access individual entries: unfortunately experimenting with this does not seem to reveal any additional data (GPS, HR, etc all still missing)
    def DownloadActivity(self, svcRecord, activity):
        # We've probably got as much information as we're going to get - we need to copy it into a Lap though.
        activity.Laps = [Lap(startTime=activity.StartTime, endTime=activity.EndTime, stats=activity.Stats)]
        return activity

#        activityID = activity.ServiceData["ActivityID"]
#        self._globalRateLimit()
#
#        streamdata = requests.get("https://api.dailymile.com/entries/" + str(activityID) + ".json", headers=self._apiHeaders(svcRecord))
#        if streamdata.status_code == 401:
#            raise APIException("No authorization to download activity", block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))
#
#        try:
#            streamdata = streamdata.json()
#        except:
#            raise APIException("Stream data returned is not JSON")
#
#        if "message" in streamdata and streamdata["message"] == "Record Not Found":
#            raise APIException("Could not find activity")
#
#        return activity

    def UploadActivity(self, serviceRecord, activity):
        logger.info("Activity tz " + str(activity.TZ) + " dt tz " + str(activity.StartTime.tzinfo) + " starttime " + str(activity.StartTime))

        if self.LastUpload is not None:
            while (datetime.now() - self.LastUpload).total_seconds() < 5:
                time.sleep(1)
                logger.debug("Inter-upload cooldown")
        source_svc = None
        if hasattr(activity, "ServiceDataCollection"):
            source_svc = str(list(activity.ServiceDataCollection.keys())[0])

        upload_id = None
        
        req = {}
        req["workout"] = {
                            "title": activity.Name if activity.Name else activity.Type,
                            "activity_type": self._activityTypeMappings[activity.Type],
                            "duration": round((activity.EndTime - activity.StartTime).total_seconds()),
                            "completed_at": activity.EndTime.astimezone(pytz.utc).strftime(self._getDateFmt())
                        }
        if activity.Notes is not None:
            req["message"] = activity.Notes

        # TODO: is there a way to set units according to user preference (not hard-coded)?  Or just refer to rule #24...
        # Swimming is the only activity type that does not accept "km" as a valid unit, and requires meters or yards
        if(activity.Type == "Swimming"):
            req["workout"]["distance"] = {
                                            "value": activity.Stats.Distance.asUnits(ActivityStatisticUnit.Meters).Value,
                                            "units": "meters"
                                        }
        else:
            req["workout"]["distance"] = {
                                            "value": activity.Stats.Distance.asUnits(ActivityStatisticUnit.Kilometers).Value,
                                            "units": "kilometers"
                                        }

        logger.debug("Req = " + str(json.dumps(req)))

        params = self._paramsIncludingAuth({}, serviceRecord)
        self._globalRateLimit()

        response = requests.post("https://api.dailymile.com/entries.json", params=params, data=json.dumps(req), headers={"Content-Type": "application/json"})

        if response.status_code != 201:
            if response.status_code == 401:
                raise APIException("No authorization to upload activity " + activity.UID + " response " + response.text + " status " + str(response.status_code), block=True, user_exception=UserException(UserExceptionType.Authorization, intervention_required=True))
            if "duplicate of activity" in response.text:
                logger.debug("Duplicate")
                self.LastUpload = datetime.now()
                return # Fine by me. The majority of these cases were caused by a dumb optimization that meant existing activities on services were never flagged as such if tapiriik didn't have to synchronize them elsewhere.
            raise APIException("Unable to upload activity " + activity.UID + " response " + response.text + " status " + str(response.status_code))

        upload_id = response.json()["id"]

        # Only then go on with uploading GPS data if it exists (Dailymile API seems to need this to be a separate step)
        if activity.CountTotalWaypoints():
            if "gpx" in activity.PrerenderedFormats:
                logger.debug("Using prerendered GPX")
                gpxData = activity.PrerenderedFormats["gpx"]
            else:
                # TODO: put the gpx back into PrerenderedFormats once there's more RAM to go around and there's a possibility of it actually being used.
                gpxData = GPXIO.Dump(activity)
            files = {"file":("tap-sync-" + activity.UID + "-" + str(os.getpid()) + ("-" + source_svc if source_svc else "") + ".gpx", gpxData)}

            upload_poll_wait = 1
            time.sleep(upload_poll_wait)
            self._globalRateLimit()

            # Add Content-Type to the headers
            headers = {"Content-Type": "application/gpx+xml"}

            response = requests.put("https://api.dailymile.com/entries/" + str(upload_id) + "/track.json", headers=headers, params=params, data=gpxData)
            if response.status_code != 201:
                logger.info("Problem uploading GPX of activity for ID: " + str(upload_id) + " - response: " + str(response.text))
                if "duplicate of activity" in response.text:
                    self.LastUpload = datetime.now()
                    logger.debug("Duplicate")
                    return # I guess we're done here?
                raise APIException("Dailymile failed while uploading GPX of activity - last status %s" % response.text)
            upload_poll_wait = min(30, upload_poll_wait * 2)
            
        self.LastUpload = datetime.now()
        return upload_id

    def DeleteCachedData(self, serviceRecord):
        cachedb.strava_cache.remove({"Owner": serviceRecord.ExternalID})
        cachedb.strava_activity_cache.remove({"Owner": serviceRecord.ExternalID})
