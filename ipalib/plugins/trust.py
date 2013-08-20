# Authors:
#     Alexander Bokovoy <abokovoy@redhat.com>
#     Martin Kosek <mkosek@redhat.com>
#
# Copyright (C) 2011  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from ipalib.plugins.baseldap import *
from ipalib.plugins.dns import dns_container_exists
from ipapython.ipautil import realm_to_suffix
from ipalib import api, Str, StrEnum, Password, _, ngettext
from ipalib import Command
from ipalib import errors
from ldap import SCOPE_SUBTREE
from time import sleep

try:
    import pysss_murmur #pylint: disable=F0401
    _murmur_installed = True
except Exception, e:
    _murmur_installed = False

try:
    import pysss_nss_idmap #pylint: disable=F0401
    _nss_idmap_installed = True
except Exception, e:
    _nss_idmap_installed = False

if api.env.in_server and api.env.context in ['lite', 'server']:
    try:
        import ipaserver.dcerpc #pylint: disable=F0401
        _bindings_installed = True
    except ImportError:
        _bindings_installed = False

__doc__ = _("""
Cross-realm trusts

Manage trust relationship between IPA and Active Directory domains.

In order to allow users from a remote domain to access resources in IPA
domain, trust relationship needs to be established. Currently IPA supports
only trusts between IPA and Active Directory domains under control of Windows
Server 2008 or later, with functional level 2008 or later.

Please note that DNS on both IPA and Active Directory domain sides should be
configured properly to discover each other. Trust relationship relies on
ability to discover special resources in the other domain via DNS records.

Examples:

1. Establish cross-realm trust with Active Directory using AD administrator
   credentials:

   ipa trust-add --type=ad <ad.domain> --admin <AD domain administrator> --password

2. List all existing trust relationships:

   ipa trust-find

3. Show details of the specific trust relationship:

   ipa trust-show <ad.domain>

4. Delete existing trust relationship:

   ipa trust-del <ad.domain>

Once trust relationship is established, remote users will need to be mapped
to local POSIX groups in order to actually use IPA resources. The mapping should
be done via use of external membership of non-POSIX group and then this group
should be included into one of local POSIX groups.

Example:

1. Create group for the trusted domain admins' mapping and their local POSIX group:

   ipa group-add --desc='<ad.domain> admins external map' ad_admins_external --external
   ipa group-add --desc='<ad.domain> admins' ad_admins

2. Add security identifier of Domain Admins of the <ad.domain> to the ad_admins_external
   group:

   ipa group-add-member ad_admins_external --external 'AD\\Domain Admins'

3. Allow members of ad_admins_external group to be associated with ad_admins POSIX group:

   ipa group-add-member ad_admins --groups ad_admins_external

4. List members of external members of ad_admins_external group to see their SIDs:

   ipa group-show ad_admins_external


GLOBAL TRUST CONFIGURATION

When IPA AD trust subpackage is installed and ipa-adtrust-install is run,
a local domain configuration (SID, GUID, NetBIOS name) is generated. These
identifiers are then used when communicating with a trusted domain of the
particular type.

1. Show global trust configuration for Active Directory type of trusts:

   ipa trustconfig-show --type ad

2. Modify global configuration for all trusts of Active Directory type and set
   a different fallback primary group (fallback primary group GID is used as
   a primary user GID if user authenticating to IPA domain does not have any other
   primary GID already set):

   ipa trustconfig-mod --type ad --fallback-primary-group "alternative AD group"

3. Change primary fallback group back to default hidden group (any group with
   posixGroup object class is allowed):

   ipa trustconfig-mod --type ad --fallback-primary-group "Default SMB Group"
""")

trust_output_params = (
    Str('trustdirection',
        label=_('Trust direction')),
    Str('trusttype',
        label=_('Trust type')),
    Str('truststatus',
        label=_('Trust status')),
)

_trust_type_dict = {1 : _('Non-Active Directory domain'),
                    2 : _('Active Directory domain'),
                    3 : _('RFC4120-compliant Kerberos realm')}
_trust_direction_dict = {1 : _('Trusting forest'),
                         2 : _('Trusted forest'),
                         3 : _('Two-way trust')}
_trust_status_dict = {True : _('Established and verified'),
                 False : _('Waiting for confirmation by remote side')}
_trust_type_dict_unknown = _('Unknown')

_trust_type_option = StrEnum('trust_type',
                        cli_name='type',
                        label=_('Trust type (ad for Active Directory, default)'),
                        values=(u'ad',),
                        default=u'ad',
                        autofill=True,
                    )

DEFAULT_RANGE_SIZE = 200000

def trust_type_string(level):
    """
    Returns a string representing a type of the trust. The original field is an enum:
      LSA_TRUST_TYPE_DOWNLEVEL  = 0x00000001,
      LSA_TRUST_TYPE_UPLEVEL    = 0x00000002,
      LSA_TRUST_TYPE_MIT        = 0x00000003
    """
    string = _trust_type_dict.get(int(level), _trust_type_dict_unknown)
    return unicode(string)

def trust_direction_string(level):
    """
    Returns a string representing a direction of the trust. The original field is a bitmask taking two bits in use
      LSA_TRUST_DIRECTION_INBOUND  = 0x00000001,
      LSA_TRUST_DIRECTION_OUTBOUND = 0x00000002
    """
    string = _trust_direction_dict.get(int(level), _trust_type_dict_unknown)
    return unicode(string)

def trust_status_string(level):
    string = _trust_status_dict.get(level, _trust_type_dict_unknown)
    return unicode(string)

class trust(LDAPObject):
    """
    Trust object.
    """
    trust_types = ('ad', 'ipa')
    container_dn = api.env.container_trusts
    object_name = _('trust')
    object_name_plural = _('trusts')
    object_class = ['ipaNTTrustedDomain']
    default_attributes = ['cn', 'ipantflatname', 'ipanttrusteddomainsid',
        'ipanttrusttype', 'ipanttrustattributes', 'ipanttrustdirection', 'ipanttrustpartner',
        'ipantauthtrustoutgoing', 'ipanttrustauthincoming', 'ipanttrustforesttrustinfo',
        'ipanttrustposixoffset', 'ipantsupportedencryptiontypes' ]
    search_display_attributes = ['cn', 'ipantflatname',
                                 'ipanttrusteddomainsid', 'ipanttrusttype' ]

    label = _('Trusts')
    label_singular = _('Trust')

    takes_params = (
        Str('cn',
            cli_name='realm',
            label=_('Realm name'),
            primary_key=True,
        ),
        Str('ipantflatname',
            cli_name='flat_name',
            label=_('Domain NetBIOS name'),
            flags=['no_create', 'no_update']),
        Str('ipanttrusteddomainsid',
            cli_name='sid',
            label=_('Domain Security Identifier'),
            flags=['no_create', 'no_update']),
        Str('ipantsidblacklistincoming*',
            csv=True,
            cli_name='sid_blacklist_incoming',
            label=_('SID blacklist incoming'),
            flags=['no_create']),
        Str('ipantsidblacklistoutgoing*',
            csv=True,
            cli_name='sid_blacklist_outgoing',
            label=_('SID blacklist outgoing'),
            flags=['no_create']),
    )

    def validate_sid_blacklists(self, entry_attrs):
        if not _bindings_installed:
            # SID validator is not available, return
            # Even if invalid SID gets in the trust entry, it won't crash
            # the validation process as it is translated to SID S-0-0
            return
        for attr in ('ipantsidblacklistincoming', 'ipantsidblacklistoutgoing'):
            values = entry_attrs.get(attr)
            if not values:
                continue
            for value in values:
                if not ipaserver.dcerpc.is_sid_valid(value):
                    raise errors.ValidationError(name=attr,
                            error=_("invalid SID: %(value)s") % dict(value=value))

def make_trust_dn(env, trust_type, dn):
    assert isinstance(dn, DN)
    if trust_type in trust.trust_types:
        container_dn = DN(('cn', trust_type), env.container_trusts, env.basedn)
        return DN(dn[0], container_dn)
    return dn

class trust_add(LDAPCreate):
    __doc__ = _('''
Add new trust to use.

This command establishes trust relationship to another domain
which becomes 'trusted'. As result, users of the trusted domain
may access resources of this domain.

Only trusts to Active Directory domains are supported right now.

The command can be safely run multiple times against the same domain,
this will cause change to trust relationship credentials on both
sides.
    ''')

    range_types = {
        u'ipa-ad-trust': unicode(_('Active Directory domain range')),
        u'ipa-ad-trust-posix': unicode(_('Active Directory trust range with '
                                        'POSIX attributes')),
                  }

    takes_options = LDAPCreate.takes_options + (
        _trust_type_option,
        Str('realm_admin?',
            cli_name='admin',
            label=_("Active Directory domain administrator"),
        ),
        Password('realm_passwd?',
            cli_name='password',
            label=_("Active directory domain administrator's password"),
            confirm=False,
        ),
        Str('realm_server?',
            cli_name='server',
            label=_('Domain controller for the Active Directory domain (optional)'),
        ),
        Password('trust_secret?',
            cli_name='trust_secret',
            label=_('Shared secret for the trust'),
            confirm=False,
        ),
        Int('base_id?',
            cli_name='base_id',
            label=_('First Posix ID of the range reserved for the trusted domain'),
        ),
        Int('range_size?',
            cli_name='range_size',
            label=_('Size of the ID range reserved for the trusted domain'),
        ),
        StrEnum('range_type?',
            label=_('Range type'),
            cli_name='range_type',
            doc=(_('Type of trusted domain ID range, one of {vals}'
                 .format(vals=', '.join(range_types.keys())))),
            values=tuple(range_types.keys()),
        ),
    )

    msg_summary = _('Added Active Directory trust for realm "%(value)s"')
    has_output_params = LDAPCreate.has_output_params + trust_output_params

    def execute(self, *keys, **options):
        full_join = self.validate_options(*keys, **options)
        old_range, range_name, dom_sid = self.validate_range(*keys, **options)
        result = self.execute_ad(full_join, *keys, **options)

        if not old_range:
            self.add_range(range_name, dom_sid, *keys, **options)

        trust_filter = "cn=%s" % result['value']
        ldap = self.obj.backend
        (trusts, truncated) = ldap.find_entries(
                         base_dn = DN(api.env.container_trusts, api.env.basedn),
                         filter = trust_filter)

        result['result'] = entry_to_dict(trusts[0][1], **options)
        result['result']['trusttype'] = [trust_type_string(result['result']['ipanttrusttype'][0])]
        result['result']['trustdirection'] = [trust_direction_string(result['result']['ipanttrustdirection'][0])]
        result['result']['truststatus'] = [trust_status_string(result['verified'])]
        del result['verified']

        return result

    def validate_options(self, *keys, **options):
        if not _bindings_installed:
            raise errors.NotFound(
                name=_('AD Trust setup'),
                reason=_(
                    'Cannot perform join operation without Samba 4 support '
                    'installed. Make sure you have installed server-trust-ad '
                    'sub-package of IPA'
                )
            )

        if not _murmur_installed and 'base_id' not in options:
            raise errors.ValidationError(
                name=_('missing base_id'),
                error=_(
                    'pysss_murmur is not available on the server '
                    'and no base-id is given.'
                )
            )

        if 'trust_type' not in options:
            raise errors.RequirementError(name=_('trust type'))

        if options['trust_type'] != u'ad':
            raise errors.ValidationError(
                name=_('trust type'),
                error=_('only "ad" is supported')
            )

        self.trustinstance = ipaserver.dcerpc.TrustDomainJoins(self.api)
        if not self.trustinstance.configured:
            raise errors.NotFound(
                name=_('AD Trust setup'),
                reason=_(
                    'Cannot perform join operation without own domain '
                    'configured. Make sure you have run ipa-adtrust-install '
                    'on the IPA server first'
                )
            )

        self.realm_server = options.get('realm_server')
        self.realm_admin = options.get('realm_admin')
        self.realm_passwd = options.get('realm_passwd')

        if self.realm_admin:
            names = self.realm_admin.split('@')

            if len(names) > 1:
                # realm admin name is in UPN format, user@realm, check that
                # realm is the same as the one that we are attempting to trust
                if keys[-1].lower() != names[-1].lower():
                    raise errors.ValidationError(
                        name=_('AD Trust setup'),
                        error=_(
                            'Trusted domain and administrator account use '
                            'different realms'
                        )
                    )
                self.realm_admin = names[0]

            if not self.realm_passwd:
                raise errors.ValidationError(
                    name=_('AD Trust setup'),
                    error=_('Realm administrator password should be specified')
                )
            return True

        return False

    def validate_range(self, *keys, **options):
        # If a range for this trusted domain already exists,
        # '--base-id' or '--range-size' options should not be specified
        range_name = keys[-1].upper() + '_id_range'
        range_type = options.get('range_type')

        try:
            old_range = api.Command['idrange_show'](range_name, raw=True)
        except errors.NotFound:
            old_range = None

        if options.get('type') == u'ad':
            if range_type and range_type not in (u'ipa-ad-trust',
                                                 u'ipa-ad-trust-posix'):
                raise errors.ValidationError(
                    name=_('id range type'),
                    error=_(
                        'Only the ipa-ad-trust and ipa-ad-trust-posix are '
                        'allowed values for --range-type when adding an AD '
                        'trust.'
                    ))

        base_id = options.get('base_id')
        range_size = options.get('range_size')

        if old_range and (base_id or range_size):
            raise errors.ValidationError(
                name=_('id range'),
                error=_(
                    'An id range already exists for this trust. '
                    'You should either delete the old range, or '
                    'exclude --base-id/--range-size options from the command.'
                )
            )

        # If a range for this trusted domain already exists,
        # domain SID must also match
        self.trustinstance.populate_remote_domain(
            keys[-1],
            self.realm_server,
            self.realm_admin,
            self.realm_passwd
        )
        dom_sid = self.trustinstance.remote_domain.info['sid']

        if old_range:
            old_dom_sid = old_range['result']['ipanttrusteddomainsid'][0]
            old_range_type = old_range['result']['iparangetype'][0]

            if old_dom_sid != dom_sid:
                raise errors.ValidationError(
                    name=_('range exists'),
                    error=_(
                        'ID range with the same name but different domain SID '
                        'already exists. The ID range for the new trusted '
                        'domain must be created manually.'
                    )
                )

            if range_type and range_type != old_range_type:
                raise errors.ValidationError(name=_('range type change'),
                    error=_('ID range for the trusted domain already exists, '
                            'but it has a different type. Please remove the '
                            'old range manually, or do not enforce type '
                            'via --range-type option.'))

        return old_range, range_name, dom_sid

    def add_range(self, range_name, dom_sid, *keys, **options):
        """
        First, we try to derive the parameters of the ID range based on the
        information contained in the Active Directory.

        If that was not successful, we go for our usual defaults (random base,
        range size 200 000, ipa-ad-trust range type).

        Any of these can be overriden by passing appropriate CLI options
        to the trust-add command.
        """

        range_size = None
        range_type = None
        base_id = None

        # First, get information about ID space from AD
        # However, we skip this step if other than ipa-ad-trust-posix
        # range type is enforced

        if options.get('range_type', None) in (None, u'ipa-ad-trust-posix'):

            # Get the base dn
            domain = keys[-1]
            basedn = realm_to_suffix(domain)

            # Search for information contained in
            # CN=ypservers,CN=ypServ30,CN=RpcServices,CN=System
            info_filter = '(objectClass=msSFU30DomainInfo)'
            info_dn = DN('CN=ypservers,CN=ypServ30,CN=RpcServices,CN=System')\
                      + basedn

            # Get the domain validator
            domain_validator = ipaserver.dcerpc.DomainValidator(self.api)
            if not domain_validator.is_configured():
                raise errors.NotFound(
                    reason=_('Cannot search in trusted domains without own '
                             'domain configured. Make sure you have run '
                             'ipa-adtrust-install on the IPA server first'))

            # KDC might not get refreshed data at the first time,
            # retry several times
            for retry in range(10):
                info_list = domain_validator.search_in_dc(domain,
                                                          info_filter,
                                                          None,
                                                          SCOPE_SUBTREE,
                                                          basedn=info_dn,
                                                          use_http=True,
                                                          quiet=True)

                if info_list:
                    info = info_list[0]
                    break
                else:
                    sleep(2)

            required_msSFU_attrs = ['msSFU30MaxUidNumber', 'msSFU30OrderNumber']

            if not info_list:
                # We were unable to gain UNIX specific info from the AD
                self.log.debug("Unable to gain POSIX info from the AD")
            else:
                if all(attr in info for attr in required_msSFU_attrs):
                    self.log.debug("Able to gain POSIX info from the AD")
                    range_type = u'ipa-ad-trust-posix'

                    max_uid = info.get('msSFU30MaxUidNumber')
                    max_gid = info.get('msSFU30MaxGidNumber', None)
                    max_id = int(max(max_uid, max_gid)[0])

                    base_id = int(info.get('msSFU30OrderNumber')[0])
                    range_size = (1 + (max_id - base_id) / DEFAULT_RANGE_SIZE)\
                                 * DEFAULT_RANGE_SIZE

        # Second, options given via the CLI options take precedence to discovery
        if options.get('range_type', None):
            range_type = options.get('range_type', None)
        elif not range_type:
            range_type = u'ipa-ad-trust'

        if options.get('range_size', None):
            range_size = options.get('range_size', None)
        elif not range_size:
            range_size = DEFAULT_RANGE_SIZE

        if options.get('base_id', None):
            base_id = options.get('base_id', None)
        elif not base_id:
            # Generate random base_id if not discovered nor given via CLI
            base_id = DEFAULT_RANGE_SIZE + (
                pysss_murmur.murmurhash3(
                    dom_sid,
                    len(dom_sid), 0xdeadbeefL
                ) % 10000
            ) * DEFAULT_RANGE_SIZE

        # Finally, add new ID range
        api.Command['idrange_add'](range_name,
                                   ipabaseid=base_id,
                                   ipaidrangesize=range_size,
                                   ipabaserid=0,
                                   iparangetype=range_type,
                                   ipanttrusteddomainsid=dom_sid)

    def execute_ad(self, full_join, *keys, **options):
        # Join domain using full credentials and with random trustdom
        # secret (will be generated by the join method)
        try:
            api.Command['trust_show'](keys[-1])
            summary = _('Re-established trust to domain "%(value)s"')
        except errors.NotFound:
            summary = self.msg_summary

        # 1. Full access to the remote domain. Use admin credentials and
        # generate random trustdom password to do work on both sides
        if full_join:
            try:
                result = self.trustinstance.join_ad_full_credentials(
                    keys[-1],
                    self.realm_server,
                    self.realm_admin,
                    self.realm_passwd
                )
            except errors.NotFound:
                error_message=_("Unable to resolve domain controller for '%s' domain. ") % (keys[-1])
                instructions=[]
                if dns_container_exists(self.obj.backend):
                    try:
                        dns_zone = api.Command.dnszone_show(keys[-1])['result']
                        if ('idnsforwardpolicy' in dns_zone) and dns_zone['idnsforwardpolicy'][0] == u'only':
                            instructions.append(_("Forward policy is defined for it in IPA DNS, "
                                                   "perhaps forwarder points to incorrect host?"))
                    except (errors.NotFound, KeyError) as e:
                        instructions.append(_("IPA manages DNS, please verify "
                                              "your DNS configuration and "
                                              "make sure that service records "
                                              "of the '%(domain)s' domain can "
                                              "be resolved. Examples how to "
                                              "configure DNS with CLI commands "
                                              "or the Web UI can be found in "
                                              "the documentation. " ) %
                                              dict(domain=keys[-1]))
                else:
                    instructions.append(_("Since IPA does not manage DNS records, ensure DNS "
                                           "is configured to resolve '%(domain)s' domain from "
                                           "IPA hosts and back.") % dict(domain=keys[-1]))
                raise errors.NotFound(reason=error_message, instructions=instructions)

            if result is None:
                raise errors.ValidationError(name=_('AD Trust setup'),
                                             error=_('Unable to verify write permissions to the AD'))

            ret = dict(
                value=self.trustinstance.remote_domain.info['dns_domain'],
                verified=result['verified']
            )
            ret['summary'] = summary % ret
            return ret


        # 2. We don't have access to the remote domain and trustdom password
        # is provided. Do the work on our side and inform what to do on remote
        # side.
        if 'trust_secret' in options:
            result = self.trustinstance.join_ad_ipa_half(
                keys[-1],
                self.realm_server,
                options['trust_secret']
            )
            ret = dict(
                value=self.trustinstance.remote_domain.info['dns_domain'],
                verified=result['verified']
            )
            ret['summary'] = summary % ret
            return ret
        raise errors.ValidationError(name=_('AD Trust setup'),
                                     error=_('Not enough arguments specified to perform trust setup'))

class trust_del(LDAPDelete):
    __doc__ = _('Delete a trust.')

    msg_summary = _('Deleted trust "%(value)s"')

    def pre_callback(self, ldap, dn, *keys, **options):
        assert isinstance(dn, DN)
        try:
            result = self.api.Command.trust_show(keys[-1])
        except errors.NotFound, e:
            self.obj.handle_not_found(*keys)
        return result['result']['dn']

class trust_mod(LDAPUpdate):
    __doc__ = _("""
    Modify a trust (for future use).

    Currently only the default option to modify the LDAP attributes is
    available. More specific options will be added in coming releases.
    """)

    msg_summary = _('Modified trust "%(value)s" '
                    '(change will be effective in 60 seconds)')

    def pre_callback(self, ldap, dn, entry_attrs, attrs_list, *keys, **options):
        assert isinstance(dn, DN)
        result = None
        try:
            result = self.api.Command.trust_show(keys[-1])
        except errors.NotFound, e:
            self.obj.handle_not_found(*keys)

        self.obj.validate_sid_blacklists(entry_attrs)

        # TODO: we found the trust object, now modify it
        return result['result']['dn']

class trust_find(LDAPSearch):
    __doc__ = _('Search for trusts.')
    has_output_params = LDAPSearch.has_output_params + trust_output_params +\
                        (Str('ipanttrusttype'),)

    msg_summary = ngettext(
        '%(count)d trust matched', '%(count)d trusts matched', 0
    )

    # Since all trusts types are stored within separate containers under 'cn=trusts',
    # search needs to be done on a sub-tree scope
    def pre_callback(self, ldap, filters, attrs_list, base_dn, scope, *args, **options):
        assert isinstance(base_dn, DN)
        return (filters, base_dn, ldap.SCOPE_SUBTREE)

    def post_callback(self, ldap, entries, truncated, *args, **options):
        if options.get('pkey_only', False):
            return truncated

        for entry in entries:
            (dn, attrs) = entry

            # Translate ipanttrusttype to trusttype if --raw not used
            if not options.get('raw', False):
                attrs['trusttype'] = trust_type_string(attrs['ipanttrusttype'][0])
                del attrs['ipanttrusttype']

        return truncated

class trust_show(LDAPRetrieve):
    __doc__ = _('Display information about a trust.')
    has_output_params = LDAPRetrieve.has_output_params + trust_output_params +\
                        (Str('ipanttrusttype'), Str('ipanttrustdirection'))

    def execute(self, *keys, **options):
        error = None
        result = None
        for trust_type in trust.trust_types:
            options['trust_show_type'] = trust_type
            try:
                result = super(trust_show, self).execute(*keys, **options)
            except errors.NotFound, e:
                result = None
                error = e
            if result:
                break
        if error or not result:
            self.obj.handle_not_found(*keys)

        return result

    def pre_callback(self, ldap, dn, entry_attrs, *keys, **options):
        assert isinstance(dn, DN)
        if 'trust_show_type' in options:
            return make_trust_dn(self.env, options['trust_show_type'], dn)

        return dn

    def post_callback(self, ldap, dn, entry_attrs, *keys, **options):

        # Translate ipanttrusttype to trusttype
        # and ipanttrustdirection to trustdirection
        # if --raw not used

        if not options.get('raw', False):
            type_str = trust_type_string(entry_attrs['ipanttrusttype'][0])
            dir_str = trust_direction_string(entry_attrs['ipanttrustdirection']
                                                        [0])
            entry_attrs['trusttype'] = [type_str]
            entry_attrs['trustdirection'] = [dir_str]
            del entry_attrs['ipanttrusttype']
            del entry_attrs['ipanttrustdirection']

        return dn

api.register(trust)
api.register(trust_add)
api.register(trust_mod)
api.register(trust_del)
api.register(trust_find)
api.register(trust_show)

_trustconfig_dn = {
    u'ad': DN(('cn', api.env.domain), api.env.container_cifsdomains, api.env.basedn),
}


class trustconfig(LDAPObject):
    """
    Trusts global configuration object
    """
    object_name = _('trust configuration')
    default_attributes = [
        'cn', 'ipantsecurityidentifier', 'ipantflatname', 'ipantdomainguid',
        'ipantfallbackprimarygroup',
    ]

    label = _('Global Trust Configuration')
    label_singular = _('Global Trust Configuration')

    takes_params = (
        Str('cn',
            label=_('Domain'),
            flags=['no_update'],
        ),
        Str('ipantsecurityidentifier',
            label=_('Security Identifier'),
            flags=['no_update'],
        ),
        Str('ipantflatname',
            label=_('NetBIOS name'),
            flags=['no_update'],
        ),
        Str('ipantdomainguid',
            label=_('Domain GUID'),
            flags=['no_update'],
        ),
        Str('ipantfallbackprimarygroup',
            cli_name='fallback_primary_group',
            label=_('Fallback primary group'),
        ),
    )

    def get_dn(self, *keys, **kwargs):
        trust_type = kwargs.get('trust_type')
        if trust_type is None:
            raise errors.RequirementError(name='trust_type')
        try:
            return _trustconfig_dn[kwargs['trust_type']]
        except KeyError:
            raise errors.ValidationError(name='trust_type',
                error=_("unsupported trust type"))

    def _normalize_groupdn(self, entry_attrs):
        """
        Checks that group with given name/DN exists and updates the entry_attrs
        """
        if 'ipantfallbackprimarygroup' not in entry_attrs:
            return

        group = entry_attrs['ipantfallbackprimarygroup']
        if isinstance(group, (list, tuple)):
            group = group[0]

        if group is None:
            return

        try:
            dn = DN(group)
            # group is in a form of a DN
            try:
                self.backend.get_entry(dn)
            except errors.NotFound:
                self.api.Object['group'].handle_not_found(group)
            # DN is valid, we can just return
            return
        except ValueError:
            # The search is performed for groups with "posixgroup" objectclass
            # and not "ipausergroup" so that it can also match groups like
            # "Default SMB Group" which does not have this objectclass.
            try:
                (dn, group_entry) = self.backend.find_entry_by_attr(
                    self.api.Object['group'].primary_key.name,
                    group,
                    ['posixgroup'],
                    [''],
                    DN(api.env.container_group, api.env.basedn))
            except errors.NotFound:
                self.api.Object['group'].handle_not_found(group)
            else:
                entry_attrs['ipantfallbackprimarygroup'] = [dn]

    def _convert_groupdn(self, entry_attrs, options):
        """
        Convert an group dn into a name. As we use CN as user RDN, its value
        can be extracted from the DN without further LDAP queries.
        """
        if options.get('raw', False):
            return

        try:
            groupdn = entry_attrs['ipantfallbackprimarygroup'][0]
        except (IndexError, KeyError):
            groupdn = None

        if groupdn is None:
            return
        assert isinstance(groupdn, DN)

        entry_attrs['ipantfallbackprimarygroup'] = [groupdn[0][0].value]

api.register(trustconfig)

class trustconfig_mod(LDAPUpdate):
    __doc__ = _('Modify global trust configuration.')

    takes_options = LDAPUpdate.takes_options + (_trust_type_option,)
    msg_summary = _('Modified "%(value)s" trust configuration')

    def pre_callback(self, ldap, dn, entry_attrs, attrs_list, *keys, **options):
        self.obj._normalize_groupdn(entry_attrs)
        return dn

    def execute(self, *keys, **options):
        result = super(trustconfig_mod, self).execute(*keys, **options)
        result['value'] = options['trust_type']
        return result

    def post_callback(self, ldap, dn, entry_attrs, *keys, **options):
        self.obj._convert_groupdn(entry_attrs, options)
        return dn

api.register(trustconfig_mod)


class trustconfig_show(LDAPRetrieve):
    __doc__ = _('Show global trust configuration.')

    takes_options = LDAPRetrieve.takes_options + (_trust_type_option,)

    def execute(self, *keys, **options):
        result = super(trustconfig_show, self).execute(*keys, **options)
        result['value'] = options['trust_type']
        return result

    def post_callback(self, ldap, dn, entry_attrs, *keys, **options):
        self.obj._convert_groupdn(entry_attrs, options)
        return dn

api.register(trustconfig_show)

if _nss_idmap_installed:
    _idmap_type_dict = {
        pysss_nss_idmap.ID_USER  : 'user',
        pysss_nss_idmap.ID_GROUP : 'group',
        pysss_nss_idmap.ID_BOTH  : 'both',
    }
    def idmap_type_string(level):
        string = _idmap_type_dict.get(int(level), 'unknown')
        return unicode(string)

class trust_resolve(Command):
    __doc__ = _('Resolve security identifiers of users and groups in trusted domains')

    takes_options = (
        Str('sids+',
            label = _('Security Identifiers (SIDs)'),
            csv = True,
        ),
    )

    has_output_params = (
        Str('name', label= _('Name')),
        Str('sid', label= _('SID')),
    )

    has_output = (
        output.ListOfEntries('result'),
    )

    def execute(self, *keys, **options):
        result = list()
        if not _nss_idmap_installed:
            return dict(result=result)
        try:
            sids = map(lambda x: str(x), options['sids'])
            xlate = pysss_nss_idmap.getnamebysid(sids)
            for sid in xlate:
                entry = dict()
                entry['sid'] = [unicode(sid)]
                entry['name'] = [unicode(xlate[sid][pysss_nss_idmap.NAME_KEY])]
                entry['type'] = [idmap_type_string(xlate[sid][pysss_nss_idmap.TYPE_KEY])]
                result.append(entry)
        except ValueError, e:
            pass

        return dict(result=result)

api.register(trust_resolve)


class adtrust_is_enabled(Command):
    NO_CLI = True

    __doc__ = _('Determine whether ipa-adtrust-install has been run on this '
                'system')

    def execute(self, *keys, **options):
        ldap = self.api.Backend.ldap2
        adtrust_dn = DN(
            ('cn', 'ADTRUST'),
            ('cn', api.env.host),
            ('cn', 'masters'),
            ('cn', 'ipa'),
            ('cn', 'etc'),
            api.env.basedn
        )

        try:
            ldap.get_entry(adtrust_dn)
        except errors.NotFound:
            return dict(result=False)

        return dict(result=True)

api.register(adtrust_is_enabled)


class compat_is_enabled(Command):
    NO_CLI = True

    __doc__ = _('Determine whether Schema Compatibility plugin is configured '
                'to serve trusted domain users and groups')

    def execute(self, *keys, **options):
        ldap = self.api.Backend.ldap2
        users_dn = DN(
            ('cn', 'users'),
            ('cn', 'Schema Compatibility'),
            ('cn', 'plugins'),
            ('cn', 'config')
        )
        groups_dn = DN(
            ('cn', 'groups'),
            ('cn', 'Schema Compatibility'),
            ('cn', 'plugins'),
            ('cn', 'config')
        )

        try:
            users_entry = ldap.get_entry(users_dn)
        except errors.NotFound:
            return dict(result=False)

        attr = users_entry.get('schema-compat-lookup-nsswitch')
        if not attr or 'user' not in attr:
            return dict(result=False)

        try:
            groups_entry = ldap.get_entry(groups_dn)
        except errors.NotFound:
            return dict(result=False)

        attr = groups_entry.get('schema-compat-lookup-nsswitch')
        if not attr or 'group' not in attr:
            return dict(result=False)

        return dict(result=True)

api.register(compat_is_enabled)


class sidgen_was_run(Command):
    """
    This command tries to determine whether the sidgen task was run during
    ipa-adtrust-install. It does that by simply checking the "editors" group
    for the presence of the ipaNTSecurityIdentifier attribute - if the
    attribute is present, the sidgen task was run.

    Since this command relies on the existence of the "editors" group, it will
    fail loudly in case this group does not exist.
    """
    NO_CLI = True

    __doc__ = _('Determine whether ipa-adtrust-install has been run with '
                'sidgen task')

    def execute(self, *keys, **options):
        ldap = self.api.Backend.ldap2
        editors_dn = DN(
            ('cn', 'editors'),
            ('cn', 'groups'),
            ('cn', 'accounts'),
            api.env.basedn
        )

        try:
            editors_entry = ldap.get_entry(editors_dn)
        except errors.NotFound:
            raise errors.NotFound(
                name=_('sidgen_was_run'),
                reason=_(
                    'This command relies on the existence of the "editors" '
                    'group, but this group was not found.'
                )
            )

        attr = editors_entry.get('ipaNTSecurityIdentifier')
        if not attr:
            return dict(result=False)

        return dict(result=True)

api.register(sidgen_was_run)
