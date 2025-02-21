import os
import json
import time
from flask import jsonify, request
from CTFd.utils import get_config
from .models import ContainerChallengeModel, ContainerInfoModel, ContainerSettingsModel, ContainerFlagModel
from .container_manager import ContainerManager, ContainerException
from CTFd.models import db, Teams, Users
from CTFd.utils.user import get_current_user


def get_settings_path():
    """Retrieve the path to settings.json"""
    # Thanks https://github.com/TheFlash2k
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


settings = json.load(open(get_settings_path()))
USERS_MODE = settings["modes"]["USERS_MODE"]
TEAMS_MODE = settings["modes"]["TEAMS_MODE"]


def settings_to_dict(settings):
    """Convert settings table records into a dictionary"""
    return {setting.key: setting.value for setting in settings}


def is_team_mode():
    """Determine if CTF is running in team mode"""
    mode = get_config("user_mode")
    return mode == TEAMS_MODE


def kill_container(container_manager, container_id):
    """Kill and remove a running container"""
    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()

    if not container:
        return jsonify({"error": "Container not found"}), 400

    try:
        container_manager.kill_container(container_id)
    except ContainerException:
        return jsonify(
            {"error": "Docker is not initialized. Please check your settings."}
        )

    db.session.delete(container)
    db.session.commit()

    return jsonify({"success": "Container killed"})


def renew_container(container_manager, chal_id, xid, is_team):
    """Extend the expiration time of an active container"""
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return jsonify({"error": "Challenge not found"}), 400

    running_container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id,
        team_id=xid if is_team else None,
        user_id=None if is_team else xid,
    ).first()

    if running_container is None:
        return jsonify({"error": "Container not found, try resetting the container."})

    try:
        running_container.expires = int(
            time.time() + container_manager.expiration_seconds
        )
        db.session.commit()
    except ContainerException:
        return jsonify({"error": "Database error occurred, please try again."})

    return jsonify(
        {
            "success": "Container renewed",
            "expires": running_container.expires,
            "hostname": container_manager.settings.get("docker_hostname", ""),
            "port": running_container.port,
            "connect": challenge.connection_type,
        }
    )


def create_container(container_manager, chal_id, xid, is_team):
    """Create a new challenge container"""
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return jsonify({"error": "Challenge not found"}), 400

    max_containers = int(container_manager.settings.get("max_containers", 3))

    # Check if user/team has reached the max container limit
    running_container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id,
        team_id=xid if is_team else None,
        user_id=None if is_team else xid,
    ).first()

    container_count = ContainerInfoModel.query.filter_by(
        team_id=xid if is_team else None,
        user_id=None if is_team else xid,
    ).count()

    if container_count >= max_containers:
        return (
            jsonify(
                {
                    "error": f"Max containers ({max_containers}) reached. Please stop a running container before starting a new one."
                }
            ),
            400,
        )

    if running_container:
        # Check if the container is still running
        try:
            if container_manager.is_container_running(running_container.container_id):
                return jsonify(
                    {
                        "status": "already_running",
                        "hostname": container_manager.settings.get(
                            "docker_hostname", ""
                        ),
                        "port": running_container.port,
                        "connect": challenge.connection_type,
                        "expires": running_container.expires,
                    }
                )
            else:
                db.session.delete(running_container)
                db.session.commit()
        except ContainerException as err:
            return jsonify({"error": str(err)}), 500

    # Start a new Docker container
    try:
        created_container = container_manager.create_container(challenge, xid, is_team)
    except ContainerException as err:
        return jsonify({"error": str(err)})

    return jsonify(
        {
            "status": "created",
            "hostname": container_manager.settings.get("docker_hostname", ""),
            "port": created_container["port"],
            "connect": challenge.connection_type,
            "expires": created_container["expires"],
        }
    )


def view_container_info(container_manager, chal_id, xid, is_team):
    """Retrieve information about a running container"""
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return jsonify({"error": "Challenge not found"}), 400

    running_container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id,
        team_id=xid if is_team else None,
        user_id=None if is_team else xid,
    ).first()

    if running_container:
        try:
            if container_manager.is_container_running(running_container.container_id):
                return jsonify(
                    {
                        "status": "already_running",
                        "hostname": container_manager.settings.get(
                            "docker_hostname", ""
                        ),
                        "port": running_container.port,
                        "connect": challenge.connection_type,
                        "expires": running_container.expires,
                    }
                )
            else:
                db.session.delete(running_container)
                db.session.commit()
        except ContainerException as err:
            return jsonify({"error": str(err)}), 500
    else:
        return jsonify({"status": "Challenge not started"})


def connect_type(chal_id):
    """Get the connection type for a challenge"""
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return jsonify({"error": "Challenge not found"}), 400

    return jsonify({"status": "Ok", "connect": challenge.connection_type})

def get_xid_and_flag():
    """
    1) Returns (x_id, submitted_flag) from the current request
    2) Raises ValueError with an error message if something is missing
    """
    user = get_current_user()
    if not user:
        raise ValueError("You must be logged in to attempt this challenge.")

    if is_team_mode():
        if not user.team_id:
            raise ValueError("You must belong to a team to solve this challenge.")
        x_id = user.team_id
    else:
        x_id = user.id

    # Parse flag from JSON or form
    data = request.get_json() or request.form
    submitted_flag = data.get("submission", "").strip()
    if not submitted_flag:
        raise ValueError("No flag provided.")

    return user, x_id, submitted_flag


def get_active_container(challenge_id, x_id):
    """
    Returns the ContainerInfoModel if found and running, else raises ValueError with a message.
    """
    container_info = ContainerInfoModel.query.filter_by(
        challenge_id=challenge_id,
        team_id=x_id if is_team_mode() else None,
        user_id=None if is_team_mode() else x_id,
    ).first()

    if not container_info:
        raise ValueError("No container is currently active for this challenge.")

    return container_info


def get_container_flag(submitted_flag):
    """
    Fetches the ContainerFlagModel for the given submitted_flag.
    Raises ValueError if not found.
    """
    container_flag = ContainerFlagModel.query.filter_by(flag=submitted_flag).first()
    if not container_flag:
        raise ValueError("Incorrect flag.")
    return container_flag


def ban_team_and_original_owner(container_flag, user, container_manager, container_info):
    """
    If we're in team mode, ban both the original and the submitting team.
    If user mode, ban both the original user and this user.
    Then kill the container, commit, and raise a ValueError for cheating.
    """
    from CTFd.models import Teams, Users

    # Flag was used => cheating => ban original owners:
    if is_team_mode():
        # original
        original_team = Teams.query.filter_by(id=container_flag.team_id).first()
        if original_team:
            original_team.banned = True
            for member in original_team.members:
                member.banned = True

        # new
        submit_team = Teams.query.filter_by(id=user.team_id).first()
        if submit_team:
            submit_team.banned = True
            for member in submit_team.members:
                member.banned = True
    else:
        # ban original user
        if container_flag.user_id:
            original_user = Users.query.filter_by(id=container_flag.user_id).first()
            if original_user:
                original_user.banned = True

        # ban this user
        user.banned = True

    db.session.commit()
    container_manager.kill_container(container_info.container_id)
    raise ValueError("Cheating detected. Both teams have been banned.")
