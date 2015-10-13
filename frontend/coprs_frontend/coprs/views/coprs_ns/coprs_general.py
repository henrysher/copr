# coding: utf-8

import os
import time
import os
import re
from six.moves.urllib.parse import urljoin

import flask
from flask import render_template, url_for
import platform
import smtplib
import sqlalchemy
from email.mime.text import MIMEText
from itertools import groupby

from coprs import app
from coprs import db
from coprs import rcp
from coprs import exceptions
from coprs import forms
from coprs import helpers
from coprs import models
from coprs.exceptions import ObjectNotFound
from coprs.logic.coprs_logic import CoprsLogic
from coprs.logic.stat_logic import CounterStatLogic
from coprs.logic.users_logic import UsersLogic
from coprs.rmodels import TimedStatEvents

from coprs.logic.complex_logic import ComplexLogic

from coprs.views.misc import login_required, page_not_found, req_with_copr, req_with_copr

from coprs.views.coprs_ns import coprs_ns
from coprs.views.groups_ns import groups_ns

from coprs.logic import builds_logic, coprs_logic, actions_logic, users_logic
from coprs.helpers import parse_package_name, generate_repo_url, CHROOT_RPMS_DL_STAT_FMT, CHROOT_REPO_MD_DL_STAT_FMT, \
    str2bool, url_for_copr_view


def url_for_copr_details(copr):
    return url_for_copr_view(
        "coprs_ns.copr_detail",
        "coprs_ns.group_copr_detail",
        copr)


def url_for_copr_edit(copr):
    return url_for_copr_view(
        "coprs_ns.copr_edit",
        "coprs_ns.group_copr_edit",
        copr)


@coprs_ns.route("/", defaults={"page": 1})
@coprs_ns.route("/<int:page>/")
def coprs_show(page=1):
    query = CoprsLogic.get_multiple()
    query = CoprsLogic.set_query_order(query, desc=True)

    paginator = helpers.Paginator(query, query.count(), page)

    coprs = paginator.sliced_query

    # flask.g.user is none when no user is logged - showing builds from everyone
    # TODO: builds_logic.BuildsLogic.get_recent_tasks(flask.g.user, 5) takes too much time, optimize sql
    # users_builds = builds_logic.BuildsLogic.get_recent_tasks(flask.g.user, 5)
    users_builds = builds_logic.BuildsLogic.get_recent_tasks(None, 5)

    return flask.render_template("coprs/show/all.html",
                                 coprs=coprs,
                                 paginator=paginator,
                                 tasks_info=ComplexLogic.get_queues_size(),
                                 users_builds=users_builds)


@coprs_ns.route("/<username>/", defaults={"page": 1})
@coprs_ns.route("/<username>/<int:page>/")
def coprs_by_owner(username=None, page=1):
    user = users_logic.UsersLogic.get(username).first()
    if not user:
        return page_not_found(
            "User {0} does not exist.".format(username))

    query = CoprsLogic.get_multiple_owned_by_username(username)
    query = CoprsLogic.filter_without_group_projects(query)
    query = CoprsLogic.set_query_order(query, desc=True)

    paginator = helpers.Paginator(query, query.count(), page)

    coprs = paginator.sliced_query

    # flask.g.user is none when no user is logged - showing builds from everyone
    users_builds = builds_logic.BuildsLogic.get_recent_tasks(flask.g.user, 5)

    return flask.render_template("coprs/show/user.html",
                                 user=user,
                                 coprs=coprs,
                                 paginator=paginator,
                                 tasks_info=ComplexLogic.get_queues_size(),
                                 users_builds=users_builds)


@coprs_ns.route("/fulltext/", defaults={"page": 1})
@coprs_ns.route("/fulltext/<int:page>/")
def coprs_fulltext_search(page=1):
    fulltext = flask.request.args.get("fulltext", "")
    try:
        query = coprs_logic.CoprsLogic.get_multiple_fulltext(fulltext)
    except ValueError as e:
        flask.flash(str(e), "error")
        return flask.redirect(flask.request.referrer or
                              flask.url_for("coprs_ns.coprs_show"))

    paginator = helpers.Paginator(query, query.count(), page,
                                  additional_params={"fulltext": fulltext})

    coprs = paginator.sliced_query
    return render_template(
        "coprs/show/fulltext.html",
        coprs=coprs,
        paginator=paginator,
        fulltext=fulltext,
        tasks_info=ComplexLogic.get_queues_size(),
    )


@coprs_ns.route("/<username>/add/")
@login_required
def copr_add(username):
    form = forms.CoprFormFactory.create_form_cls()()

    return flask.render_template("coprs/add.html", form=form)


@coprs_ns.route("/g/<group_name>/add/")
@login_required
def group_copr_add(group_name):
    group = ComplexLogic.get_group_by_name_safe(group_name)
    form = forms.CoprFormFactory.create_form_cls()()

    return flask.render_template(
        "coprs/group_add.html", form=form, group=group)


@coprs_ns.route("/g/<group_name>/new/", methods=["POST"])
@login_required
def group_copr_new(group_name):
    group = ComplexLogic.get_group_by_name_safe(group_name)
    form = forms.CoprFormFactory.create_form_cls(group=group)()

    if form.validate_on_submit():
        copr = coprs_logic.CoprsLogic.add(
            flask.g.user,
            name=form.name.data,
            homepage=form.homepage.data,
            contact=form.contact.data,
            repos=form.repos.data.replace("\n", " "),
            selected_chroots=form.selected_chroots,
            description=form.description.data,
            instructions=form.instructions.data,
            disable_createrepo=form.disable_createrepo.data,
            build_enable_net=form.build_enable_net.data,
            group_id=group.id
        )

        db.session.add(copr)
        db.session.commit()
        after_the_project_creation(copr, form)

        return flask.redirect(url_for_copr_details(copr))
    else:
        return flask.render_template("coprs/group_add.html", form=form, group=group)


@coprs_ns.route("/<username>/new/", methods=["POST"])
@login_required
def copr_new(username):
    """
    Receive information from the user on how to create its new copr
    and create it accordingly.
    """

    form = forms.CoprFormFactory.create_form_cls()()
    if form.validate_on_submit():
        copr = coprs_logic.CoprsLogic.add(
            flask.g.user,
            name=form.name.data,
            homepage=form.homepage.data,
            contact=form.contact.data,
            repos=form.repos.data.replace("\n", " "),
            selected_chroots=form.selected_chroots,
            description=form.description.data,
            instructions=form.instructions.data,
            disable_createrepo=form.disable_createrepo.data,
            build_enable_net=form.build_enable_net.data,
        )

        db.session.commit()
        after_the_project_creation(copr, form)

        return flask.redirect(url_for_copr_details(copr))
    else:
        return flask.render_template("coprs/add.html", form=form)


def after_the_project_creation(copr, form):
    flask.flash("New project has been created successfully.", "success")
    _check_rpmfusion(copr.repos)
    if form.initial_pkgs.data:
        pkgs = form.initial_pkgs.data.replace("\n", " ").split(" ")

        # validate (and skip bad) urls
        bad_urls = []
        for pkg in pkgs:
            if not re.match("^.*\.src\.rpm$", pkg):
                bad_urls.append(pkg)
                flask.flash("Bad url: {0} (skipped)".format(pkg))
        for bad_url in bad_urls:
            pkgs.remove(bad_url)

        if not pkgs:
            flask.flash("No initial packages submitted")
        else:
            # build each package as a separate build
            for pkg in pkgs:
                builds_logic.BuildsLogic.add(
                    flask.g.user,
                    pkgs=pkg,
                    copr=copr,
                    enable_net=form.build_enable_net.data
                )

            db.session.commit()
            flask.flash("Initial packages were successfully submitted "
                        "for building.")


@coprs_ns.route("/<username>/<coprname>/report-abuse")
@req_with_copr
def copr_report_abuse(copr):
    return render_copr_report_abuse(copr)


@coprs_ns.route("/g/<group_name>/<coprname>/report-abuse")
@req_with_copr
def group_copr_report_abuse(copr):
    return render_copr_report_abuse(copr)


def render_copr_report_abuse(copr):
    form = forms.CoprLegalFlagForm()
    return render_template("coprs/report_abuse.html", copr=copr, form=form)


@coprs_ns.route("/g/<group_name>/<coprname>/")
@req_with_copr
def group_copr_detail(copr):
    return render_copr_detail(copr)


@coprs_ns.route("/<username>/<coprname>/")
@req_with_copr
def copr_detail(copr):
    if copr.is_a_group_project:
        return flask.redirect(url_for_copr_details(copr))
    return render_copr_detail(copr)


def render_copr_detail(copr):
    repo_dl_stat = CounterStatLogic.get_copr_repo_dl_stat(copr)
    form = forms.CoprLegalFlagForm()
    repos_info = {}
    for chroot in copr.active_chroots:
        # chroot_rpms_dl_stat_key = CHROOT_REPO_MD_DL_STAT_FMT.format(
        #     copr_user=copr.owner.name,
        #     copr_project_name=copr.name,
        #     copr_chroot=chroot.name,
        # )
        chroot_rpms_dl_stat_key = CHROOT_RPMS_DL_STAT_FMT.format(
            copr_user=copr.owner.name,
            copr_project_name=copr.name,
            copr_chroot=chroot.name,
        )
        chroot_rpms_dl_stat = TimedStatEvents.get_count(
            rconnect=rcp.get_connection(),
            name=chroot_rpms_dl_stat_key,
        )

        if chroot.name_release not in repos_info:
            repos_info[chroot.name_release] = {
                "name_release": chroot.name_release,
                "name_release_human": chroot.name_release_human,
                "os_release": chroot.os_release,
                "os_version": chroot.os_version,
                "arch_list": [chroot.arch],
                "repo_file": "{}-{}-{}.repo".format(copr.owner.name, copr.name, chroot.name_release),
                "dl_stat": repo_dl_stat[chroot.name_release],
                "rpm_dl_stat": {
                    chroot.arch: chroot_rpms_dl_stat
                }
            }
        else:
            repos_info[chroot.name_release]["arch_list"].append(chroot.arch)
            repos_info[chroot.name_release]["rpm_dl_stat"][chroot.arch] = chroot_rpms_dl_stat
    repos_info_list = sorted(repos_info.values(), key=lambda rec: rec["name_release"])
    builds = builds_logic.BuildsLogic.get_multiple_by_copr(copr=copr).limit(1).all()
    return flask.render_template(
        "coprs/detail/overview.html",
        copr=copr,
        user=flask.g.user,
        form=form,
        repo_dl_stat=repo_dl_stat,
        repos_info_list=repos_info_list,
        latest_build=builds[0] if len(builds) == 1 else None,
    )


@coprs_ns.route("/<username>/<coprname>/permissions/")
@req_with_copr
def copr_permissions(copr):
    permissions = coprs_logic.CoprPermissionsLogic.get_for_copr(copr).all()
    if flask.g.user:
        user_perm = flask.g.user.permissions_for_copr(copr)
    else:
        user_perm = None

    permissions_applier_form = None
    permissions_form = None

    # generate a proper form for displaying
    if flask.g.user:
        if flask.g.user.can_edit(copr):
            permissions_form = forms.PermissionsFormFactory.create_form_cls(
                permissions)()
        else:
            # https://github.com/ajford/flask-wtf/issues/58
            permissions_applier_form = \
                forms.PermissionsApplierFormFactory.create_form_cls(
                    user_perm)(formdata=None)

    return flask.render_template(
        "coprs/detail/permissions.html",
        copr=copr,
        permissions_form=permissions_form,
        permissions_applier_form=permissions_applier_form,
        permissions=permissions,
        current_user_permissions=user_perm)


def render_copr_edit(copr, form, view):
    if not form:
        form = forms.CoprFormFactory.create_form_cls(
            copr.mock_chroots)(obj=copr)
    return flask.render_template(
        "coprs/detail/edit.html",
        copr=copr, form=form, view=view)


@coprs_ns.route("/g/<group_name>/<coprname>/edit/")
@login_required
@req_with_copr
def group_copr_edit(copr, form=None):
    return render_copr_edit(copr, form, 'coprs_ns.group_copr_update')


@coprs_ns.route("/<username>/<coprname>/edit/")
@login_required
@req_with_copr
def copr_edit(copr, form=None):
    return render_copr_edit(copr, form, 'coprs_ns.copr_update')


def _check_rpmfusion(repos):
    if "rpmfusion" in repos:
        message = flask.Markup('Using rpmfusion as dependency is nearly always wrong. Please see <a href="https://fedorahosted.org/copr/wiki/UserDocs#WhatIcanbuildinCopr">What I can build in Copr</a>.')
        flask.flash(message, "error")


def process_copr_update(copr, form):
    copr.name = form.name.data
    copr.homepage = form.homepage.data
    copr.contact = form.contact.data
    copr.repos = form.repos.data.replace("\n", " ")
    copr.description = form.description.data
    copr.instructions = form.instructions.data
    copr.disable_createrepo = form.disable_createrepo.data
    copr.build_enable_net = form.build_enable_net.data
    coprs_logic.CoprChrootsLogic.update_from_names(
        flask.g.user, copr, form.selected_chroots)
    try:
        # form validation checks for duplicates
        coprs_logic.CoprsLogic.update(flask.g.user, copr)
    except (exceptions.ActionInProgressException,
            exceptions.InsufficientRightsException) as e:

        flask.flash(str(e), "error")
        db.session.rollback()
    else:
        flask.flash("Project has been updated successfully.", "success")
        db.session.commit()
    _check_rpmfusion(copr.repos)


@coprs_ns.route("/g/<group_name>/<coprname>/update/", methods=["POST"])
@login_required
@req_with_copr
def group_copr_update(copr):
    form = forms.CoprFormFactory.create_form_cls()()

    if form.validate_on_submit():
        process_copr_update(copr, form)
        return flask.redirect(url_for(
            "coprs_ns.group_copr_detail",
            group_name=copr.group.name, coprname=copr.name
        ))

    else:
        return group_copr_edit(copr.group.name, copr.name, form)


@coprs_ns.route("/<username>/<coprname>/update/", methods=["POST"])
@login_required
@req_with_copr
def copr_update(copr):
    form = forms.CoprFormFactory.create_form_cls()()

    if form.validate_on_submit():
        process_copr_update(copr, form)
        return flask.redirect(url_for_copr_details(copr))
    else:
        return copr_edit(copr.owner.username, copr.name, form)


@coprs_ns.route("/<username>/<coprname>/permissions_applier_change/",
                methods=["POST"])
@login_required
@req_with_copr
def copr_permissions_applier_change(copr):
    permission = coprs_logic.CoprPermissionsLogic.get(copr, flask.g.user).first()
    applier_permissions_form = \
        forms.PermissionsApplierFormFactory.create_form_cls(permission)()

    if copr.owner == flask.g.user:
        flask.flash("Owner cannot request permissions for his own project.", "error")
    elif applier_permissions_form.validate_on_submit():
        # we rely on these to be 0 or 1 from form. TODO: abstract from that
        if permission is not None:
            old_builder = permission.copr_builder
            old_admin = permission.copr_admin
        else:
            old_builder = 0
            old_admin = 0
        new_builder = applier_permissions_form.copr_builder.data
        new_admin = applier_permissions_form.copr_admin.data
        coprs_logic.CoprPermissionsLogic.update_permissions_by_applier(
            flask.g.user, copr, permission, new_builder, new_admin)
        db.session.commit()
        flask.flash(
            "Successfuly updated permissions for project '{0}'."
            .format(copr.name))
        admin_mails = [copr.owner.mail]
        for perm in copr.copr_permissions:
            # this 2 means that his status (admin) is approved
            if perm.copr_admin == 2:
                admin_mails.append(perm.user.mail)

        # sending emails
        if flask.current_app.config.get("SEND_EMAILS", False):
            for mail in admin_mails:
                msg = MIMEText(
                    "{6} is asking for these permissions:\n\n"
                    "Builder: {0} -> {1}\nAdmin: {2} -> {3}\n\n"
                    "Project: {4}\nOwner: {5}".format(
                        helpers.PermissionEnum(old_builder),
                        helpers.PermissionEnum(new_builder),
                        helpers.PermissionEnum(old_admin),
                        helpers.PermissionEnum(new_admin),
                        copr.name, copr.owner.name, flask.g.user.name))

                msg["Subject"] = "[Copr] {0}: {1} is asking permissons".format(copr.name, flask.g.user.name)
                msg["From"] = "root@{0}".format(platform.node())
                msg["To"] = mail
                s = smtplib.SMTP("localhost")
                s.sendmail("root@{0}".format(platform.node()), mail, msg.as_string())
                s.quit()

    return flask.redirect(flask.url_for("coprs_ns.copr_detail",
                                        username=copr.owner.name,
                                        coprname=copr.name))


@coprs_ns.route("/<username>/<coprname>/update_permissions/", methods=["POST"])
@login_required
@req_with_copr
def copr_update_permissions(copr):
    permissions = copr.copr_permissions
    permissions_form = forms.PermissionsFormFactory.create_form_cls(
        permissions)()

    if permissions_form.validate_on_submit():
        # we don't change owner (yet)
        try:
            # if admin is changing his permissions, his must be changed last
            # so that we don't get InsufficientRightsException
            permissions.sort(
                cmp=lambda x, y: -1 if y.user_id == flask.g.user.id else 1)
            for perm in permissions:
                old_builder = perm.copr_builder
                old_admin = perm.copr_admin
                new_builder = permissions_form[
                    "copr_builder_{0}".format(perm.user_id)].data
                new_admin = permissions_form[
                    "copr_admin_{0}".format(perm.user_id)].data
                coprs_logic.CoprPermissionsLogic.update_permissions(
                    flask.g.user, copr, perm, new_builder, new_admin)
                if flask.current_app.config.get("SEND_EMAILS", False) and \
                        (old_builder is not new_builder or old_admin is not new_admin):

                    msg = MIMEText(
                        "Your permissions have changed:\n\n"
                        "Builder: {0} -> {1}\nAdmin: {2} -> {3}\n\n"
                        "Project: {4}\nOwner: {5}".format(
                            helpers.PermissionEnum(old_builder),
                            helpers.PermissionEnum(new_builder),
                            helpers.PermissionEnum(old_admin),
                            helpers.PermissionEnum(new_admin),
                            copr.name, copr.owner.name))

                    msg["Subject"] = "[Copr] {0}: Your permissions have changed".format(copr.name)
                    msg["From"] = "root@{0}".format(platform.node())
                    msg["To"] = perm.user.mail
                    s = smtplib.SMTP("localhost")
                    s.sendmail("root@{0}".format(platform.node()), perm.user.mail, msg.as_string())
                    s.quit()
        # for now, we don't check for actions here, as permissions operation
        # don't collide with any actions
        except exceptions.InsufficientRightsException as e:
            db.session.rollback()
            flask.flash(str(e), "error")
        else:
            db.session.commit()
            flask.flash("Project permissions were updated successfully.", "success")

    return flask.redirect(url_for_copr_details(copr))


@coprs_ns.route("/id/<copr_id>/createrepo/", methods=["POST"])
@login_required
def copr_createrepo(copr_id):
    copr = ComplexLogic.get_copr_by_id_safe(copr_id)

    chroots = [c.name for c in copr.active_chroots]
    actions_logic.ActionsLogic.send_createrepo(
        username=copr.owner.name, coprname=copr.name,
        chroots=chroots)

    db.session.commit()
    flask.flash("Repository metadata will be regenerated in a few minutes ...")
    return flask.redirect(url_for_copr_details(copr))


def process_delete(copr, url_on_error, url_on_success):
    form = forms.CoprDeleteForm()
    if form.validate_on_submit():

        try:
            ComplexLogic.delete_copr(copr)
        except (exceptions.ActionInProgressException,
                exceptions.InsufficientRightsException) as e:

            db.session.rollback()
            flask.flash(str(e), "error")
            return flask.redirect(url_on_error)
        else:
            db.session.commit()
            flask.flash("Project has been deleted successfully.")
            return flask.redirect(url_on_success)
    else:
        return render_template("coprs/detail/delete.html", form=form, copr=copr)


@coprs_ns.route("/<username>/<coprname>/delete/", methods=["GET", "POST"])
@login_required
@req_with_copr
def copr_delete(copr):
    return process_delete(
        copr,
        url_on_error=url_for("coprs_ns.copr_detail",
                             username=copr.owner.name, coprname=copr.name),
        url_on_success=url_for("coprs_ns.coprs_by_owner", username=copr.owner.username)
    )


@coprs_ns.route("/g/<group_name>/<coprname>/delete/", methods=["GET", "POST"])
@login_required
@req_with_copr
def group_copr_delete(copr):

    return process_delete(
        copr,
        url_on_error=url_for('coprs_ns.group_copr_detail',
                             group_name=copr.group.name, coprname=copr.name),
        url_on_success=url_for('groups_ns.list_projects_by_group',
                               group_name=copr.group.name)
    )


@coprs_ns.route("/<username>/<coprname>/legal_flag/", methods=["POST"])
@login_required
@req_with_copr
def copr_legal_flag(copr):
    contact_info = "{} <>".format(copr.owner.username, copr.owner.mail)
    return process_legal_flag(contact_info, copr)


@coprs_ns.route("/g/<group_name>/<coprname>/legal_flag/", methods=["POST"])
@login_required
@req_with_copr
def group_copr_legal_flag(copr):
    contact_info = "group managed project, fas name: {}".format(copr.group.name)
    return process_legal_flag(contact_info, copr)


def process_legal_flag(contact_info, copr):
    form = forms.CoprLegalFlagForm()
    legal_flag = models.LegalFlag(raise_message=form.comment.data,
                                  raised_on=int(time.time()),
                                  copr=copr,
                                  reporter=flask.g.user)
    db.session.add(legal_flag)
    db.session.commit()
    send_to = app.config["SEND_LEGAL_TO"] or ["root@localhost"]
    hostname = platform.node()
    navigate_to = "\nNavigate to http://{0}{1}".format(
        hostname, flask.url_for("admin_ns.legal_flag"))
    contact = "\nContact on owner is: {}".format(contact_info)
    reported_by = "\nReported by {0} <{1}>".format(flask.g.user.name,
                                                   flask.g.user.mail)
    try:
        msg = MIMEText(
            form.comment.data + navigate_to + contact + reported_by, "plain")
    except UnicodeEncodeError:
        msg = MIMEText(form.comment.data.encode(
            "utf-8") + navigate_to + contact + reported_by, "plain", "utf-8")
    msg["Subject"] = "Legal flag raised on {0}".format(copr.name)
    msg["From"] = "root@{0}".format(hostname)
    msg["To"] = ", ".join(send_to)
    s = smtplib.SMTP("localhost")
    s.sendmail("root@{0}".format(hostname), send_to, msg.as_string())
    s.quit()
    flask.flash("Admin has been noticed about your report"
                " and will investigate the project shortly.")
    return flask.redirect(url_for_copr_details(copr))


@coprs_ns.route("/<username>/<coprname>/repo/<name_release>/", defaults={"repofile": None})
@coprs_ns.route("/<username>/<coprname>/repo/<name_release>/<repofile>")
def generate_repo_file(username, coprname, name_release, repofile):
    """ Generate repo file for a given repo name.
        Reponame = username-coprname """
    # This solution is used because flask splits off the last part after a
    # dash, therefore user-re-po resolves to user-re/po instead of user/re-po
    # FAS usernames may not contain dashes, so this construction is safe.

    # support access to the group projects using @-notation
    # todo: remove when yum/dnf plugin is updated to use new url schema
    if username.startswith("@"):
        return group_generate_repo_file(group_name=username[1:], coprname=coprname,
                                        name_release=name_release, repofile=repofile)

    copr = ComplexLogic.get_copr_safe(username, coprname)
    return render_generate_repo_file(copr, name_release, repofile)


@coprs_ns.route("/g/<group_name>/<coprname>/repo/<name_release>/", defaults={"repofile": None})
@coprs_ns.route("/g/<group_name>/<coprname>/repo/<name_release>/<repofile>")
@req_with_copr
def group_generate_repo_file(copr, name_release, repofile):
    """ Generate repo file for a given repo name.
        Reponame = username-coprname """
    # This solution is used because flask splits off the last part after a
    # dash, therefore user-re-po resolves to user-re/po instead of user/re-po
    # FAS usernames may not contain dashes, so this construction is safe.

    return render_generate_repo_file(copr, name_release, repofile)


def render_generate_repo_file(copr, name_release, repofile):

    # we need to check if we really got name release or it's a full chroot (caused by old dnf plugin)
    if name_release in [c.name for c in copr.active_chroots]:
        chroot = [c for c in copr.active_chroots if c.name == name_release][0]
        kwargs = dict(coprname=copr.name, name_release=chroot.name)
        if copr.is_a_group_project:
            fixed_url = url_for("coprs_ns.group_generate_repo_file",
                                group_name=copr.group.name, **kwargs)
        else:
            fixed_url = url_for("coprs_ns.generate_repo_file",
                                username=copr.owner.username, **kwargs)
        return flask.redirect(fixed_url)

    expected = "{}-{}-{}.repo".format(copr.owner.username, copr.name, name_release)
    if repofile is not None and repofile != expected:
        raise ObjectNotFound(
            "Repository filename does not match expected: {}".format(repofile))

    mock_chroot = coprs_logic.MockChrootsLogic.get_from_name(name_release, noarch=True).first()
    if not mock_chroot:
        raise ObjectNotFound("Chroot {} does not exist".format(name_release))

    url = ""
    for build in copr.builds:
        if build.results:
            url = build.results
            break
    if not url:
        raise ObjectNotFound(
            "Repository not initialized: No finished builds in {}/{}."
            .format(copr.owner.username, copr.name))

    # add trainling slash
    url = os.path.join(url, '')
    repo_url = generate_repo_url(mock_chroot, url)
    pubkey_url = urljoin(url, "pubkey.gpg")
    response = flask.make_response(
        flask.render_template("coprs/copr.repo", copr=copr, url=repo_url, pubkey_url=pubkey_url))
    response.mimetype = "text/plain"
    response.headers["Content-Disposition"] = \
        "filename={0}.repo".format(copr.repo_name)
    return response


@coprs_ns.route("/<username>/<coprname>/rpm/<name_release>/<rpmfile>")
def copr_repo_rpm_file(username, coprname, name_release, rpmfile):
    try:
        PACKAGES_DIR = "/usr/share/copr/repo_rpm_storage"  # @TODO Move to the config file
        with open(os.path.join(PACKAGES_DIR, rpmfile), "rb") as rpm:
            response = flask.make_response(rpm.read())
            response.mimetype = "application/x-rpm"
            response.headers["Content-Disposition"] = \
                "filename={0}".format(rpmfile)
            return response
    except IOError:
        return flask.render_template("404.html")


def render_monitor(copr, detailed=False):
    monitor = builds_logic.BuildsMonitorLogic.get_monitor_data(copr)
    oses = [chroot.os for chroot in copr.active_chroots_sorted]
    oses_grouped = [(len(list(group)), key) for key, group in groupby(oses)]
    archs = [chroot.arch for chroot in copr.active_chroots_sorted]
    if detailed:
        template = "coprs/detail/monitor/detailed.html"
    else:
        template = "coprs/detail/monitor/simple.html"
    return flask.render_template(template,
                                 copr=copr,
                                 monitor=monitor,
                                 oses=oses_grouped,
                                 archs=archs)


@coprs_ns.route("/<username>/<coprname>/monitor/")
@req_with_copr
def copr_build_monitor(copr):
    detailed = str2bool(flask.request.args.get("detailed"))
    return render_monitor(copr, detailed)


@coprs_ns.route("/g/<group_name>/<coprname>/monitor/")
@req_with_copr
def group_copr_build_monitor(copr):
    detailed = bool(flask.request.args.get("detailed", False))
    return render_monitor(copr, detailed)


