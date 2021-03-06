$(document).ready(function() {

    var postTypes = ['article', 'note', 'checkin', 'reply', 'share', 'like'];

    $(postTypes).each(function (i, type) {

        $('#new-' + type).click(function (event) {
            event.preventDefault();

            $.get('/admin/new?type=' + type, '',
                  function(result) {
                      $('#composition-area').empty().append(result);
                  });

        });
    });


    $('.admin-post-controls-arrow').click(function (event) {
        var arrow = $(event.currentTarget);
        var post = arrow.closest('article');
        var controls = post.find('.admin-post-controls');

        controls.css('display', 'inline');
        arrow.replaceWith(controls);
        /*
        if (controls.css('display') == 'none') {
            controls.css('display', 'inline');
        } else {
            controls.css('display', 'none');
        }*/

        return false;
    });
});
